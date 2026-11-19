import json
import os
from collections.abc import Iterable
from pathlib import Path
from tempfile import NamedTemporaryFile


def _write_atomic(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(contents)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def write_json_atomic(path: Path, value: object) -> None:
    contents = json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
    )
    _write_atomic(path, contents + "\n")


def upsert_jsonl(path: Path, rows: Iterable[dict], key: str) -> None:
    indexed = {}
    if path.exists():
        with path.open(encoding="utf-8") as existing:
            for line in existing:
                if line.strip():
                    row = json.loads(line)
                    indexed[row[key]] = row
    for row in rows:
        indexed[row[key]] = row
    write_jsonl_atomic(path, (indexed[key] for key in sorted(indexed)))


def write_jsonl_atomic(path, rows):
    contents = "".join(
        json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
        for row in rows
    )
    _write_atomic(path, contents)
