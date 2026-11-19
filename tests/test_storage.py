import pytest

from radar.storage import upsert_jsonl, write_json_atomic


def test_rerun_no_duplicates(tmp_path):
    path = tmp_path / "state" / "events.jsonl"
    upsert_jsonl(path, [{"id": "b", "revision": 1}, {"id": "a", "revision": 1}], "id")
    upsert_jsonl(path, [{"id": "b", "revision": 2}], "id")
    assert path.read_text().splitlines() == [
        '{"id":"a","revision":1}',
        '{"id":"b","revision":2}',
    ]


def test_failed_replace_preserves_existing_file(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    write_json_atomic(path, {"version": 1})
    original = path.read_bytes()

    def fail_replace(*args):
        raise OSError("replace failed")

    monkeypatch.setattr("radar.storage.os.replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        write_json_atomic(path, {"version": 2})
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]
