"""RADAR_TEST_DATABASE_URL must point to a disposable PostgreSQL database."""

import os
from pathlib import Path

import pytest

from radar import ingest_store
from radar.collect import collect
from test_collectors import HN, NOW, source


def test_import_dry_run_preserves_operational_files():
    assert ingest_store.inventory(Path("data"))["events"] > 0


def test_remote_ingest_idempotent_and_leased(tmp_path):
    if "RADAR_TEST_DATABASE_URL" not in os.environ:
        pytest.skip("requires disposable PostgreSQL")
    import psycopg

    with psycopg.connect(os.environ["RADAR_TEST_DATABASE_URL"]) as db:
        db.execute("CREATE SCHEMA radar_test")
        db.execute("SET LOCAL search_path TO radar_test")
        ingest_store.migrate(db)
        results = collect([source("hn", "hackernews")], NOW, fetch=lambda *a, **k: HN)
        for _ in range(2):
            ingest_store.save_batch(
                db, results, [], {"hn": {"last_complete_at": NOW.isoformat()}}, NOW
            )
        assert db.execute("select count(*) from discovery_edge").fetchone() == (1,)
        job = ingest_store.claim_job(db)
        assert job and ingest_store.claim_job(db) is None
        db.rollback()


def test_old_job_retains_source_snapshot():
    if "RADAR_TEST_DATABASE_URL" not in os.environ:
        pytest.skip("requires disposable PostgreSQL")
    import psycopg

    with psycopg.connect(os.environ["RADAR_TEST_DATABASE_URL"]) as db:
        db.execute("CREATE SCHEMA radar_old_job")
        db.execute("SET LOCAL search_path TO radar_old_job")
        ingest_store.migrate(db)
        src = source("hn", "hackernews")
        for payload in (HN, HN.replace(b"example.org/release", b"z.org/new")):
            results = collect([src], NOW, fetch=lambda *a, **k: payload)
            ingest_store.save_batch(db, results, [], {}, NOW)
        job = ingest_store.claim_job(db)
        assert job[0] == job[4]["link_target_url"]
        db.rollback()


def test_concurrent_writer_cannot_replace_newer_cursor():
    if "RADAR_TEST_DATABASE_URL" not in os.environ:
        pytest.skip("requires disposable PostgreSQL")
    import psycopg

    with psycopg.connect(os.environ["RADAR_TEST_DATABASE_URL"]) as db:
        db.execute("CREATE SCHEMA radar_stale")
        db.execute("SET LOCAL search_path TO radar_stale")
        ingest_store.migrate(db)
        ingest_store.save_batch(
            db, [], [], {"src": {"last_complete_at": "newer"}}, NOW, expected={}
        )
        with pytest.raises(ValueError, match="changed"):
            ingest_store.save_batch(db, [], [], {}, NOW, expected={})
        db.rollback()
