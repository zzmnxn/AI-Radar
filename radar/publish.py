import json
import os
import shutil
from contextlib import contextmanager
from fcntl import LOCK_EX, flock
from hashlib import sha256
from pathlib import Path
from tempfile import mkdtemp

from .storage import write_json_atomic

MANIFEST = Path("manifest.json")
BUDGET = Path("state/llm-budget.json")
JOURNAL = "transaction.json"
MANAGED = ("briefings", "index", "runs", "state")


class SnapshotConflict(RuntimeError):
    pass


class StageFailure(RuntimeError):
    pass


def injection(stage):
    if os.environ.get("RADAR_FAIL_STAGE") == stage:
        raise StageFailure(stage)


def _contents(path):
    return path.read_bytes() if path.is_file() else None


def _files(data):
    return {
        path.relative_to(data)
        for name in MANAGED
        for path in (data / name).rglob("*")
        if path.is_file()
        and ".staging" not in path.relative_to(data).parts
        and path.relative_to(data) != BUDGET
    }


def _recover(data, journal):
    if not journal.exists():
        return
    record = json.loads(journal.read_bytes())
    name = record["work"]
    paths = [Path(value) for value in record["paths"]]
    if (
        not name.startswith("run-")
        or Path(name).name != name
        or any(
            not path.parts
            or path.parts[0] not in MANAGED
            or ".." in path.parts
            or path == BUDGET
            for path in paths
        )
    ):
        raise ValueError("Invalid publish recovery journal")
    work = journal.parent / name
    current = _contents(data / MANIFEST)
    if current is None or sha256(current).hexdigest() != record["manifest_hash"]:
        for name in paths:
            destination = data / name
            backup = work / "previous" / name
            if backup.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                restored = work / "restore"
                shutil.copyfile(backup, restored)
                restored.replace(destination)
            else:
                destination.unlink(missing_ok=True)
    journal.unlink()
    shutil.rmtree(work, ignore_errors=True)


def _commit(data, stage, journal, original):
    if _contents(data / MANIFEST) != original:
        raise SnapshotConflict("Manifest changed during run")
    manifest = (stage / MANIFEST).read_bytes()
    paths = sorted(
        path
        for path in _files(data) | _files(stage)
        if _contents(data / path) != _contents(stage / path)
    )
    if not paths and manifest == original:
        return
    work = stage.parent
    for name in paths:
        source = data / name
        if source.is_file():
            backup = work / "previous" / name
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, backup)
    write_json_atomic(
        journal,
        {
            "work": work.name,
            "paths": [str(path) for path in paths],
            "manifest_hash": sha256(manifest).hexdigest(),
        },
    )
    try:
        for name in paths:
            source, target = stage / name, data / name
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                source.replace(target)
            else:
                target.unlink(missing_ok=True)
        if _contents(data / MANIFEST) != original:
            raise SnapshotConflict("Manifest changed during publish")
        (stage / MANIFEST).replace(data / MANIFEST)
    finally:
        _recover(data, journal)


def reset_stage(data, stage):
    for name in MANAGED:
        shutil.rmtree(stage / name, ignore_errors=True)
    for path in _files(data):
        destination = stage / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(data / path, destination)


@contextmanager
def transaction(data, *, write=True):
    staging = data / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    with (staging / "publish.lock").open("a") as lock:
        flock(lock, LOCK_EX)
        journal = staging / JOURNAL
        _recover(data, journal)
        for abandoned in staging.glob("run-*"):
            if abandoned.is_dir() and not abandoned.is_symlink():
                shutil.rmtree(abandoned, ignore_errors=True)
        if not write:
            yield data
            return
        original = _contents(data / MANIFEST)
        work = Path(mkdtemp(prefix="run-", dir=staging))
        stage = work / "data"
        stage.mkdir()
        try:
            reset_stage(data, stage)
            if original is not None:
                (stage / MANIFEST).write_bytes(original)
            yield stage
            _commit(data, stage, journal, original)
        finally:
            if not journal.exists():
                shutil.rmtree(work, ignore_errors=True)
