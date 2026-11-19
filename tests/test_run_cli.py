import subprocess
import sys

from radar.assemble import briefing_path
from radar.slice import validate_snapshot


def test_no_llm_cli_publishes_preview(tmp_path):
    subprocess.run(
        [sys.executable, "-m", "radar", "run", "--no-llm", "--data", str(tmp_path)],
        check=True,
    )
    manifest = validate_snapshot(tmp_path)
    assert manifest.published
    path = briefing_path(manifest.published.date)
    assert "테스트용" in (tmp_path / path).read_text()


def test_explicit_dates_force_and_failure_preserve_snapshot(tmp_path):
    import json
    import os

    data = tmp_path / "data"
    output = tmp_path / "github-output"

    def run(day, *flags, fail=None):
        env = {**os.environ, "GITHUB_OUTPUT": str(output)}
        if fail:
            env["RADAR_FAIL_STAGE"] = fail
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "radar",
                "run",
                "--data",
                str(data),
                "--no-llm",
                "--date",
                day,
                *flags,
            ],
            env=env,
            capture_output=True,
        )

    assert run("2025-12-31").returncode == 0
    first = validate_snapshot(data, references=True)
    first_files = {name: (data / name).read_bytes() for name in first.published.files}
    analyses = list((data / "state/analyses").glob("*.jsonl"))
    assert len(analyses) == 1
    initial_lines = analyses[0].read_bytes()
    manifest_before = (data / "manifest.json").read_bytes()
    assert run("2025-12-31").stdout.strip().endswith(b"skipped")
    assert (data / "manifest.json").read_bytes() == manifest_before
    assert analyses[0].read_bytes() == initial_lines
    assert len((data / "runs/2025-12.jsonl").read_text().splitlines()) == 1

    assert run("2025-12-31", "--force").returncode == 0
    current = validate_snapshot(data, references=True)
    assert current.published.date == first.published.date
    assert current.published.run_id != first.published.run_id
    latest = (data / briefing_path(current.published.date)).read_bytes()
    assert (
        json.loads(latest)["cards"]
        == json.loads(first_files["briefings/2025-12-31.json"])["cards"]
    )
    current_files = {
        name: (data / name).read_bytes() for name in current.published.files
    }
    for stage in (
        "collect",
        "group",
        "tag",
        "prefilter",
        "enrich",
        "evaluate",
        "verify",
        "select",
        "assemble",
        "prune",
        "publish",
    ):
        failure = run("2025-12-31", "--force", fail=stage)
        assert failure.returncode == 1, failure.stderr
        after = validate_snapshot(data, references=True)
        assert after.published == current.published
        assert after.last_run.failed_stage == stage
        assert {
            name: (data / name).read_bytes() for name in current_files
        } == current_files
    assert output.read_text().splitlines()[-1] == "outcome=failed"
    assert run("2026-01-01").returncode == 0
    last = validate_snapshot(data, references=True)
    assert "index/2025.json" in last.published.files
    assert "index/2026.json" in last.published.files


def test_real_runs_reject_dates_older_than_the_backfill_window(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "radar",
            "run",
            "--data",
            str(tmp_path),
            "--date",
            "2023-11-19",
        ],
        capture_output=True,
    )
    assert result.returncode == 2
    assert b"at most 7 days" in result.stderr
    assert not (tmp_path / "manifest.json").exists()
