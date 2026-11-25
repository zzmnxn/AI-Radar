"""Private collection state. Public briefings remain validated file snapshots."""

import hashlib
import json
import sys
import os
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from .deduplicate import KST, load_events
from .enrich import enrich
from .http import FetchError
from .models import EventRecord, RawItem
from .normalize import canonical_url
from .storage import write_json_atomic, write_jsonl_atomic


def _root_certificates():
    """Bundled libpq builds may not find the OS trust store; name the file."""
    import ssl

    paths = ssl.get_default_verify_paths()
    for candidate in (
        paths.cafile,
        paths.openssl_cafile,
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/ssl/cert.pem",
    ):
        if candidate and os.path.isfile(candidate):
            return candidate
    return "system"


def connect():
    import psycopg

    from psycopg.conninfo import conninfo_to_dict

    dsn = os.environ["DATABASE_URL"]
    host = conninfo_to_dict(dsn).get("host", "")
    options = (
        {}
        if host in {"localhost", "127.0.0.1", "::1"} or host.startswith("/")
        else {"sslmode": "verify-full", "sslrootcert": _root_certificates()}
    )
    try:
        return psycopg.connect(dsn, connect_timeout=10, **options)
    except psycopg.OperationalError as exc:
        # The message can carry the host or user, so only a category is printed.
        text = str(exc).lower()
        category = next(
            (
                name
                for name, words in (
                    ("auth", ("password", "authentication", "role")),
                    ("tls", ("ssl", "certificate", "tls")),
                    ("dns", ("translate host", "name or service", "resolve")),
                    ("timeout", ("timeout", "timed out")),
                )
                if any(word in text for word in words)
            ),
            "other",
        )
        print(f"ingest-db connect failed: {category}", file=sys.stderr)
        raise


def migrate(db):
    db.execute((Path(__file__).parent / "migrations/001_ingest.sql").read_text())


def health(data):
    path = data / "state/source_health.json"
    return json.loads(path.read_text()) if path.exists() else {}


def inventory(data):
    return {"events": len(load_events(data)), "sources": len(health(data))}


def save_state(db, events, cursors):
    from psycopg.types.json import Jsonb

    with db.cursor() as cur:
        cur.executemany(
            "INSERT INTO ingest_event VALUES (%s,%s) ON CONFLICT(event_id) DO UPDATE SET payload=excluded.payload",
            [(e.event_id, Jsonb(e.model_dump(mode="json"))) for e in events],
        )
        cur.executemany(
            "INSERT INTO source_cursor VALUES (%s,%s) ON CONFLICT(source_id) DO UPDATE SET health=excluded.health",
            [(key, Jsonb(value)) for key, value in cursors.items()],
        )


def save_batch(db, results, events, cursors, now, expected=None):
    from psycopg.types.json import Jsonb

    # Compare the snapshot read before network collection, under the writer lock.
    db.execute("SELECT pg_advisory_xact_lock(1701873251)")
    if expected is not None and expected != dict(
        db.execute("SELECT source_id,health FROM source_cursor").fetchall()
    ):
        raise ValueError("Collection snapshot changed; retry from the newer cursor")
    items, edges, jobs = [], [], []
    for result in results:
        for item in result.items:
            url = canonical_url(item.link_target_url or item.canonical_url)
            version = hashlib.sha256(
                f"{item.source_updated_at}|{item.snippet}".encode()
            ).hexdigest()
            items.append(
                (
                    item.item_id,
                    item.source_id,
                    Jsonb(item.model_dump(mode="json")),
                    url,
                    version,
                    item.first_seen_at,
                    now,
                )
            )
            edges.append((item.item_id, url))
            if item.link_target_url or result.source.id in {
                "geeknews",
                "simon-willison",
                "latent-space",
            }:
                jobs.append(
                    (url, version, item.item_id, Jsonb(item.model_dump(mode="json")))
                )
    with db.cursor() as cur:
        cur.executemany(
            """INSERT INTO source_item VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(item_id)
                        DO UPDATE SET payload=excluded.payload,target_url=excluded.target_url,version=excluded.version,last_seen=greatest(source_item.last_seen,excluded.last_seen)
                        WHERE excluded.last_seen >= source_item.last_seen""",
            items,
        )
        cur.executemany(
            "INSERT INTO document VALUES (%s) ON CONFLICT DO NOTHING",
            [(url,) for url in sorted({url for _, url in edges})],
        )
        cur.executemany(
            "INSERT INTO discovery_edge VALUES (%s,%s) ON CONFLICT DO NOTHING", edges
        )
        cur.executemany(
            "INSERT INTO fetch_job(url,version,item_id,input) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            jobs,
        )
    save_state(db, events, cursors)


def claim_job(db):
    db.execute("""UPDATE fetch_job SET status='failed',token=NULL
        WHERE status='running' AND attempts>=3 AND available_at<=now()""")
    token = uuid4()
    return db.execute(
        """WITH next AS (
        SELECT url,version FROM fetch_job WHERE status IN ('pending','running') AND attempts < 3 AND available_at <= now()
        ORDER BY available_at,url,version LIMIT 1 FOR UPDATE SKIP LOCKED
        ) UPDATE fetch_job j SET token=%s,attempts=attempts+1,status='running',
        available_at=now()+interval '2 minutes' FROM next n
        WHERE (j.url,j.version)=(n.url,n.version)
        RETURNING j.url,j.version,j.token,j.attempts,j.input""",
        (token,),
    ).fetchone()


def fetch_descriptions(now, limit=3):
    for _ in range(limit):
        with connect() as db:
            job = claim_job(db)
        if not job:
            return
        url, version, token, attempts, payload = job
        try:
            summary = enrich(RawItem.model_validate(payload), now=now).text[:300]
            status = "done"
        except FetchError:
            summary, status = None, "failed" if attempts >= 3 else "pending"
        with connect() as db:
            db.execute(
                """UPDATE fetch_job SET status=%s,excerpt=%s,available_at=now()+interval '1 hour'
                       WHERE url=%s AND version=%s AND token=%s""",
                (status, summary, url, version, token),
            )


def recent_descriptions(db, now):
    rows = db.execute(
        """SELECT s.item_id,s.payload->>'snippet',j.excerpt
        FROM source_item s LEFT JOIN fetch_job j
        ON (s.target_url,s.version)=(j.url,j.version) AND j.status='done'
        WHERE s.last_seen >= %s""",
        (now - timedelta(days=7),),
    )
    return {
        key: " ".join(filter(None, (snippet, excerpt)))
        for key, snippet, excerpt in rows
    }


def export_state(db, data):
    events = defaultdict(list)
    for (payload,) in db.execute("SELECT payload FROM ingest_event ORDER BY event_id"):
        event = EventRecord.model_validate(payload)
        events[event.first_seen_at.astimezone(KST).strftime("%Y-%m")].append(payload)
    for month, rows in events.items():
        write_jsonl_atomic(data / "state/events" / f"{month}.jsonl", rows)
    write_json_atomic(
        data / "state/source_health.json",
        dict(db.execute("SELECT source_id,health FROM source_cursor").fetchall()),
    )


def compare(db, data):
    local = {e.event_id: e.model_dump(mode="json") for e in load_events(data)}
    remote = dict(db.execute("SELECT event_id,payload FROM ingest_event").fetchall())
    cursors = dict(db.execute("SELECT source_id,health FROM source_cursor").fetchall())
    return remote == local and cursors == health(data)


def command(mode, data):
    if mode == "dry-run":
        print(json.dumps(inventory(data)))
        return
    with connect() as db:
        if mode == "migrate":
            migrate(db)
        elif mode == "stats":
            size, items, pending = (
                db.execute("""SELECT pg_database_size(current_database()),
                (SELECT count(*) FROM source_item),(SELECT count(*) FROM fetch_job WHERE status='pending')""").fetchone()
            )
            print(
                json.dumps(
                    {"database_bytes": size, "items": items, "pending_fetches": pending}
                )
            )
        elif mode == "import":
            db.execute("SELECT pg_advisory_xact_lock(1701873251)")
            if (
                not compare(db, data)
                and db.execute(
                    "SELECT EXISTS(SELECT 1 FROM ingest_event UNION ALL SELECT 1 FROM source_cursor)"
                ).fetchone()[0]
            ):
                raise ValueError("Import target contains divergent state")
            save_state(db, load_events(data), health(data))
        elif mode == "compare":
            if not compare(db, data):
                raise ValueError("Database differs from snapshot")
        elif mode == "export":
            if (data / "state/events").exists() or (
                data / "state/source_health.json"
            ).exists():
                raise ValueError("Export requires a fresh destination")
            export_state(db, data)
