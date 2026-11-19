import subprocess
import sys
import pytest

from radar.publish import transaction


@pytest.mark.parametrize("commit_completed", [False, True])
def test_killed_publisher_is_recovered(tmp_path, commit_completed):
    (tmp_path / "manifest.json").write_bytes(b"old manifest")
    (tmp_path / "state").mkdir()
    (tmp_path / "state/value").write_bytes(b"old state")
    code = """
import os
import sys
from pathlib import Path
from radar import publish

original = Path.replace
def interrupt(path, target):
    if Path(target) == Path(sys.argv[1]) / "manifest.json":
        if sys.argv[2] == "True":
            original(path, target)
        os._exit(91)
    return original(path, target)
with publish.transaction(Path(sys.argv[1])) as stage:
    (stage / "state/value").write_bytes(b"new state")
    (stage / "manifest.json").write_bytes(b"new manifest")
    Path.replace = interrupt
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), str(commit_completed)]
    )
    assert result.returncode == 91
    assert (tmp_path / "state/value").read_bytes() == b"new state"
    with transaction(tmp_path, write=False):
        expected = b"new" if commit_completed else b"old"
        assert (tmp_path / "manifest.json").read_bytes() == expected + b" manifest"
        assert (tmp_path / "state/value").read_bytes() == expected + b" state"


def test_manifest_change_refuses_publish(tmp_path):
    from radar.publish import SnapshotConflict

    path = tmp_path / "manifest.json"
    path.write_bytes(b"old")
    with pytest.raises(SnapshotConflict):
        with transaction(tmp_path) as stage:
            (stage / "manifest.json").write_bytes(b"our update")
            path.write_bytes(b"another update")
    assert path.read_bytes() == b"another update"
    assert not (tmp_path / ".staging/transaction.json").exists()
