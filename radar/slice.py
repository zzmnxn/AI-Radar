import os
from collections import Counter
from functools import partial
from datetime import date, datetime, timedelta, timezone
from fcntl import LOCK_EX, flock
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from .analysis import analyze, recent_analyses, save_analysis, weighted_total
from .assemble import assemble, briefing_path, update_index
from .deduplicate import MergeConfig, load_events, record_events
from .github import star_growth_7d
from .enrich import enrich
from .http import FetchError, check_link
from .models import (
    AnalysisRecord,
    Briefing,
    Card,
    LastRun,
    Manifest,
    PublishedSnapshot,
    RawItem,
    SourceCounts,
    Usage,
    YearIndex,
)
from .prefilter import (
    build_candidates,
    freshness,
    github_stars,
    primary_ref,
)
from .publish import injection
from .queue import QUEUE, RETRY_WINDOW, fetch_failure, transition
from .select import SHOWN_DAYS, recent_shown, select, shown_rows
from .storage import write_json_atomic
from .verify import verify


def _demo_judge(rows):
    return ["supported"] * len(rows)


def _official_ref(event):
    primary = primary_ref(event)
    primary_url = primary.link_target_url or primary.url
    return next(
        (
            member.ref
            for member in event.members
            if member.role == "official"
            and (member.ref.link_target_url or member.ref.url) != primary_url
        ),
        None,
    )


def _card_from_analysis(item, source, record, event, pool):
    if record.verdict == "rejected":
        return None
    evaluation = record.eval
    sources = [
        dict(
            source_id=item.source_id,
            source_class=item.source_class,
            org=source.org,
            url=record.evidence[0].url,
            title=item.title,
        )
    ]
    official = _official_ref(event)
    if len(record.evidence) > 1 and official:
        sources.append(
            dict(
                source_id=official.source_id,
                source_class=official.source_class,
                org=official.org,
                url=record.evidence[1].url,
                title=official.title,
            )
        )
    return Card(
        card_id=f"card_{record.analysis_id[:12]}",
        event_id=event.event_id,
        revision=event.revision,
        analysis_id=record.analysis_id,
        title_ko=evaluation.title_ko,
        category=evaluation.category,
        tags=evaluation.tags,
        published_at=item.published_at,
        sources=sources,
        what=evaluation.what,
        why=evaluation.why if any(c.role == "why" for c in evaluation.claims) else [],
        how=evaluation.how,
        availability=evaluation.availability,
        access=evaluation.access,
        verification_summary={
            "level": record.verification.level,
            "claims": evaluation.claims,
            "uncertainty": evaluation.uncertainty,
        },
        score=weighted_total(evaluation.scores),
        pool=pool or ("explore" if source.discovery_mode == "open" else "impact"),
    )


def _raw_from_ref(ref, collected):
    current = collected.get(ref.item_id)
    return RawItem(
        **ref.model_dump(exclude={"org", "snippet_sha256", "metrics"}),
        metrics=current.metrics if current else ref.metrics.model_dump(),
    )


def validate_snapshot(data: Path, *, references=False) -> Manifest:
    manifest = Manifest.model_validate_json((data / "manifest.json").read_bytes())
    recent_cards = []
    if manifest.published:
        snapshot = manifest.published
        expected_path = briefing_path(snapshot.date)
        if expected_path not in snapshot.files:
            raise ValueError("Missing current briefing")
        for name, digest in snapshot.files.items():
            if name.startswith("briefings/"):
                day = date.fromisoformat(Path(name).stem)
                if name != briefing_path(day) or day > snapshot.date:
                    raise ValueError("Unexpected briefing path")
                contents = (data / name).read_bytes()
                if sha256(contents).hexdigest() != digest:
                    raise ValueError("Briefing hash mismatch")
                briefing = Briefing.model_validate_json(contents)
                if briefing.date != day or (
                    day == snapshot.date
                    and (briefing.run_id, briefing.generated_at)
                    != (snapshot.run_id, snapshot.generated_at)
                ):
                    raise ValueError("Briefing metadata differs from the manifest")
                if references and day >= snapshot.date - timedelta(days=SHOWN_DAYS):
                    recent_cards.extend(briefing.cards)
            elif name.startswith("index/"):
                year = Path(name).stem
                if (
                    len(year) != 4
                    or not year.isascii()
                    or not year.isdecimal()
                    or name != f"index/{year}.json"
                ):
                    raise ValueError("Unexpected index path")
                contents = (data / name).read_bytes()
                if sha256(contents).hexdigest() != digest:
                    raise ValueError("Index hash mismatch")
                for key, entry in YearIndex.model_validate_json(contents).root.items():
                    day = date.fromisoformat(key)
                    if (
                        key != day.isoformat()
                        or key[:4] != year
                        or entry.path != briefing_path(day)
                        or entry.path not in snapshot.files
                    ):
                        raise ValueError(
                            "Index entry does not reference a published briefing"
                        )
            else:
                raise ValueError("Unexpected snapshot path")
    if recent_cards:
        wanted = {card.analysis_id for card in recent_cards}
        records = {}
        for path in sorted((data / "state/analyses").glob("*.jsonl")):
            for line in path.read_bytes().splitlines():
                if line.strip():
                    record = AnalysisRecord.model_validate_json(line)
                    if record.analysis_id in wanted:
                        records[record.analysis_id] = record
        events = {event.event_id: event for event in load_events(data)}
        for card in recent_cards:
            record = records.get(card.analysis_id)
            event = events.get(card.event_id)
            if (
                record is None
                or record.verdict == "rejected"
                or (record.event_id, record.revision) != (card.event_id, card.revision)
                or event is None
                or not any(
                    revision.revision == card.revision
                    for revision in event.revision_history
                )
            ):
                raise ValueError(f"Broken briefing reference: {card.card_id}")
    return manifest


def _tag_stage(stats):
    stats["stage"] = "tag"
    injection("tag")


def run_slice(
    results,
    now,
    data: Path,
    *,
    evaluate,
    analyzer_version,
    limit,
    fetch=enrich,
    events=None,
    emerging=(),
    candidate_settings=None,
    link_check=None,
    force=False,
    stats=None,
    saved_descriptions=None,
):
    if limit < 1:
        raise ValueError("The candidate limit must be positive")
    staging = data / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    with (staging / "publish.lock").open("a") as lock:
        flock(lock, LOCK_EX)
        if events is None:
            if stats is not None:
                stats["stage"] = "group"
                injection("group")
            sources = {result.source.id: result.source for result in results}
            grouped = record_events(
                (item for result in results for item in result.items),
                data,
                MergeConfig(),
                now=now,
                orgs={source.id: source.org for source in sources.values()},
                trust={source.id: source.trust for source in sources.values()},
                active_sources=len(results),
                before_trends=(lambda: _tag_stage(stats))
                if stats is not None
                else None,
            )
            events, emerging = grouped.observed, grouped.emerging
        return _run_slice(
            results,
            now,
            data,
            events,
            emerging,
            evaluate=evaluate,
            analyzer_version=analyzer_version,
            limit=limit,
            fetch=fetch,
            candidate_settings=candidate_settings,
            link_check=link_check,
            force=force,
            stats=stats,
            saved_descriptions=saved_descriptions,
        )


def _run_slice(
    results,
    now,
    data,
    events,
    emerging,
    *,
    evaluate,
    analyzer_version,
    limit,
    fetch,
    candidate_settings,
    link_check,
    force,
    stats,
    saved_descriptions,
):
    def enter(name):
        if stats is not None:
            stats["stage"] = name
            injection(name)

    day = now.astimezone(ZoneInfo("Asia/Seoul")).date()
    run_id = uuid4().hex
    manifest_path = data / "manifest.json"
    previous = manifest_path.read_bytes() if manifest_path.exists() else None
    manifest = validate_snapshot(data) if previous else None
    published = manifest.published if manifest else None
    last_success = manifest.last_success_at if manifest else None
    counts = SourceCounts(
        active=len(results),
        complete=sum(result.completeness == "complete" for result in results),
        partial=sum(result.completeness == "partial" for result in results),
        failed=sum(result.completeness == "failed" for result in results),
    )

    queue = {}

    def finish(outcome, stage=None, snapshot=published, message=None):
        if message is not None:
            waiting = sum(entry.status == "deferred" for entry in queue.values())
            message += f" deferred={waiting}"
        if (manifest_path.read_bytes() if manifest_path.exists() else None) != previous:
            raise RuntimeError("Manifest changed during run")
        updated = Manifest(
            published=snapshot,
            last_success_at=now if outcome == "ok" else last_success,
            last_run=LastRun(
                run_id=run_id,
                date=day,
                started_at=now,
                finished_at=max(now, datetime.now(timezone.utc)),
                outcome=outcome,
                failed_stage=stage,
                message=message,
                sources=counts,
            ),
        )
        write_json_atomic(manifest_path, updated.model_dump(mode="json"))
        return updated

    if published and (published.date > day or (published.date == day and not force)):
        return finish("skipped")
    if not results or counts.failed * 2 >= counts.active:
        return finish("failed", "collect")

    enter("group")
    if stats is not None:
        stats["observed_ids"] = sorted(e.event_id for e in events)
    if candidate_settings and (events or saved_descriptions):
        events = load_events(data) or events
    if not events:
        return finish("failed", "group")

    enter("prefilter")
    shown = recent_shown(data, day, published)
    raw_count = sum(len(result.items) for result in results)
    if stats is not None:
        stats.update(raw=raw_count, events=len(events))
    sources = {result.source.id: result.source for result in results}
    collected = {item.item_id: item for result in results for item in result.items}
    queue_path = data / "state/analysis_queue.json"
    deferred = 0
    summary = None
    cached = {}

    def save_queue():
        write_json_atomic(queue_path, QUEUE.dump_python(queue, mode="json"))

    def update_queue(event, reason):
        entry = transition(queue.get(event.event_id), event.revision, now, reason)
        queue[event.event_id] = entry
        return entry.status == "deferred"

    if candidate_settings:
        mode = candidate_settings.pipeline.judge_mode
        if mode == "strict":
            analyzer_version = f"{analyzer_version}-strict"
        judge = evaluate.judge
        link_check = link_check or check_link
        queue = _recent_queue(queue_path, events, now)
        cached = recent_analyses(data, now, events, analyzer_version)
        if force and published and day == published.date:
            queue = {
                key: entry
                for key, entry in queue.items()
                if not (entry.status == "done" and key in cached)
            }
        remaining, estimate, reserve, calls = evaluate.candidate_budget()
        snippets = {key: item.snippet for key, item in collected.items()}
        snippets.update(saved_descriptions or {})
        plan = build_candidates(
            events,
            sources,
            now,
            candidate_settings,
            descriptions={
                event.event_id: " ".join(
                    snippets.get(m.ref.item_id, "") for m in event.members
                )
                for event in events
            },
            budget_left=remaining,
            estimate=estimate,
            headline_reserve=reserve,
            call_slots=min(limit, calls),
            queue=queue,
            shown={(row["event_id"], row["revision"]) for row in shown},
            emerging_terms=(item.tag for item in emerging),
            growth=partial(
                star_growth_7d, now=now, token=os.environ.get("GITHUB_TOKEN")
            ),
            current_stars=github_stars(results),
            cached=set(cached),
        )
        for candidate in plan.deferred:
            update_queue(candidate.event, "budget")
        deferred = len(plan.deferred)
        candidates = [(candidate.event, candidate.pool) for candidate in plan.selected]
        pools = Counter(candidate.pool for candidate in plan.selected)
        growth_count = sum(
            any(reason.startswith("star_growth_7d:") for reason in candidate.reasons)
            for candidate in plan.selected
        )
        summary = (
            f"interest={pools['interest']} impact={pools['impact']} "
            f"explore={pools['explore']} star_growth={growth_count}"
        )
        if plan.excluded:
            dropped = ",".join(
                f"{reason}x{count}" for reason, count in sorted(plan.excluded.items())
            )
            summary += f" prefilter_excluded={dropped}"
    else:
        mode = "normal"
        # The offline preview uses synthetic evidence and judgment.
        judge = _demo_judge
        link_check = link_check or (lambda url: True)
        fresh = [
            event
            for event in events
            if primary_ref(event).source_id in sources
            and now - timedelta(hours=48) <= freshness(event) <= now
        ]
        ranked = sorted(
            fresh, key=lambda event: (freshness(event), event.event_id), reverse=True
        )
        candidates = [(event, None) for event in ranked[:limit]]
    if stats is not None:
        stats["candidate_ids"] = [e.event_id for e, _ in candidates]
        stats["candidates"] = len(candidates)
        stats["pools"] = dict(Counter(pool or "impact" for _, pool in candidates))
        stats["deferred"] = sum(row.status == "deferred" for row in queue.values())
        stats["expired"] = sum(row.status == "expired" for row in queue.values())
    if not candidates:
        enter("verify")
        if candidate_settings and (queue or queue_path.exists()):
            save_queue()
        budget_waiting = any(
            entry.status == "deferred" and entry.reason == "budget"
            for entry in queue.values()
        )
        if any(entry.status == "deferred" for entry in queue.values()):
            return finish(
                "failed", "budget" if budget_waiting else "verify", message=summary
            )
    cards = []
    completed = []
    analyzed = 0
    rejected = Counter()
    llm_errors = Counter()
    examined = []
    for event, pool in candidates:
        item = _raw_from_ref(primary_ref(event), collected)
        record = cached.get(event.event_id)
        if record is None:
            enter("enrich")
            try:
                doc = fetch(item, now=now)
            except FetchError as exc:
                if candidate_settings:
                    deferred += update_queue(event, fetch_failure(exc))
                    save_queue()
                continue
            official = _official_ref(event) if candidate_settings else None
            corroboration = None
            if official:
                try:
                    corroboration = fetch(_raw_from_ref(official, collected), now=now)
                except FetchError:
                    pass
            enter("evaluate")
            evaluation = (
                evaluate(item, doc, corroboration=corroboration)
                if corroboration
                else evaluate(item, doc)
            )
            if evaluation is None:
                if failure := getattr(evaluate, "last_failure", None):
                    detail = getattr(evaluate, "last_error", None)
                    llm_errors[f"{failure}[{detail}]" if detail else failure] += 1
                if candidate_settings and evaluate.last_failure:
                    deferred += update_queue(event, evaluate.last_failure)
                    save_queue()
                continue
            docs = [doc, corroboration] if corroboration else [doc]
            enter("verify")
            checked = verify(evaluation, docs, judge, link_check, mode=mode)
            if checked is None:
                if failure := getattr(evaluate, "last_failure", None):
                    detail = getattr(evaluate, "last_error", None)
                    llm_errors[
                        f"judge:{failure}[{detail}]" if detail else f"judge:{failure}"
                    ] += 1
                if candidate_settings:
                    deferred += update_queue(
                        event, evaluate.last_failure or "schema_error"
                    )
                    save_queue()
                continue
            usage = (
                evaluate.last_usage
                if candidate_settings
                else Usage(input_tokens=0, output_tokens=0)
            )
            record = analyze(event, analyzer_version, checked, usage, now)
        enter("verify")
        analyzed += 1
        try:
            card = _card_from_analysis(
                item, sources[item.source_id], record, event, pool
            )
        except ValidationError:
            card = None
            record = record.model_copy(
                update={"verdict": "rejected", "rejection_reason": "schema_invalid"}
            )
        if record.rejection_reason:
            rejected[record.rejection_reason] += 1
        if candidate_settings:
            if event.event_id not in cached or record is not cached[event.event_id]:
                save_analysis(data, record)
            completed.append(event)
        if stats is not None:
            examined.append(record.verification)
        if card:
            if not candidate_settings:
                save_analysis(data, record)
            cards.append(card)
    if stats is not None:
        stats.update(
            analyzed=analyzed,
            accepted=len(cards),
            deferred=sum(row.status == "deferred" for row in queue.values()),
            expired=sum(row.status == "expired" for row in queue.values()),
            quote_found_rate=sum(row.quote_found_rate for row in examined)
            / len(examined)
            if examined
            else 0,
            supported_rate=sum(row.supported_rate for row in examined) / len(examined)
            if examined
            else 0,
        )
    if llm_errors:
        errors = ",".join(
            f"{reason}x{count}" for reason, count in sorted(llm_errors.items())
        )
        summary = " ".join(filter(None, [summary, f"llm_errors={errors}"]))
    if rejected:
        breakdown = " ".join(
            f"{reason}={count}" for reason, count in sorted(rejected.items())
        )
        summary = " ".join(filter(None, [summary, breakdown]))
    if candidates and not analyzed:
        return finish(
            "failed",
            "budget"
            if any(
                e.reason == "budget" and e.status == "deferred" for e in queue.values()
            )
            else "verify",
            message=summary,
        )

    enter("select")
    selected = select(
        cards,
        shown,
        explore_min_score=candidate_settings.pipeline.explore_min_score
        if candidate_settings
        else 70,
        min_score=candidate_settings.pipeline.min_card_score
        if candidate_settings
        else 0,
    )
    enter("assemble")
    if stats is not None:
        stats["selected"] = len(selected)
    briefing = assemble(
        day=day,
        run_id=run_id,
        now=now,
        counts=dict(
            raw=raw_count,
            events=len(events),
            candidates=len(candidates),
            analyzed=analyzed,
            accepted=len(cards),
            deferred=deferred,
        ),
        cards=selected,
        emerging=emerging,
        headline=evaluate.headline if candidate_settings else None,
    )
    enter("publish")
    manifest = _publish_archive(
        data,
        published,
        briefing,
        shown,
        lambda snapshot: finish("ok", snapshot=snapshot, message=summary),
    )
    if candidate_settings:
        for event in completed:
            update_queue(event, None)
        save_queue()
    return manifest


def _recent_queue(path, events, now):
    if not path.exists():
        return {}
    current = {event.event_id: event for event in events}
    queue = {}
    for key, entry in QUEUE.validate_json(path.read_bytes()).items():
        event = current.get(key)
        if event is None or entry.revision > event.revision:
            continue
        age = now - freshness(event)
        if age < timedelta(0) or age > RETRY_WINDOW + timedelta(days=1):
            continue
        if entry.revision != event.revision:
            if entry.status == "deferred" and age <= RETRY_WINDOW:
                queue[key] = transition(None, event.revision, now, "budget").model_copy(
                    update={"status": "queued", "reason": None}
                )
        elif age > RETRY_WINDOW:
            if entry.status not in {"done", "failed"}:
                queue[key] = entry.model_copy(
                    update={"status": "expired", "next_attempt_after": None}
                )
        else:
            queue[key] = entry
    return queue


def _publish_archive(data, published, briefing, shown, commit):
    files = dict(published.files) if published else {}
    indexes = {}
    existing_indexes = [name for name in files if name.startswith("index/")]
    if published and not existing_indexes:
        # Seed the index from the previous layout, where the manifest only
        # referenced the latest briefing.
        for path in sorted((data / "briefings").glob("????-??-??.json")):
            day = date.fromisoformat(path.stem)
            if day > published.date:
                continue
            contents = path.read_bytes()
            past = Briefing.model_validate_json(contents)
            if past.date != day:
                raise ValueError("Archive date does not match its path")
            name = f"index/{day.year:04}.json"
            indexes[name] = update_index(indexes.get(name, {}), past)
            files[briefing_path(day)] = sha256(contents).hexdigest()

    current_index = f"index/{briefing.date.year:04}.json"
    for name in existing_indexes:
        rows = YearIndex.model_validate_json((data / name).read_bytes()).root
        missing = [
            entry for entry in rows.values() if "titles" not in entry.model_fields_set
        ]
        for entry in missing:
            past = Briefing.model_validate_json((data / entry.path).read_bytes())
            rows = update_index(rows, past)
        if missing or name == current_index:
            indexes[name] = rows
    indexes[current_index] = update_index(indexes.get(current_index, {}), briefing)
    relative = briefing_path(briefing.date)
    with TemporaryDirectory(dir=data / ".staging") as work:
        stage = Path(work)
        write_json_atomic(stage / relative, briefing.model_dump(mode="json"))
        Briefing.model_validate_json((stage / relative).read_bytes())
        for name, rows in indexes.items():
            write_json_atomic(stage / name, YearIndex(rows).model_dump(mode="json"))
            YearIndex.model_validate_json((stage / name).read_bytes())
        shown_name = "state/shown.json"
        cutoff = (briefing.date - timedelta(days=SHOWN_DAYS)).isoformat()
        recent = [row for row in shown if row["date"] > cutoff]
        write_json_atomic(stage / shown_name, [*recent, *shown_rows(briefing)])
        for name in (relative, *indexes):
            files[name] = sha256((stage / name).read_bytes()).hexdigest()
        snapshot = PublishedSnapshot(
            date=briefing.date,
            run_id=briefing.run_id,
            generated_at=briefing.generated_at,
            files=files,
        )
        moved = []
        try:
            for name in (relative, *indexes, shown_name):
                destination = data / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                backup = stage / "previous" / name if destination.exists() else None
                if backup:
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    backup.write_bytes(destination.read_bytes())
                (stage / name).replace(destination)
                moved.append((destination, backup))
            return commit(snapshot)
        except Exception:
            for destination, backup in reversed(moved):
                if backup:
                    backup.replace(destination)
                else:
                    destination.unlink()
            raise
