import argparse
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from .collect import collect, load_sources
from .deduplicate import load_merge_config, record_events
from .demo import demo_inputs
from .health import collect_with_health
from .llm import ModelConfig, OpenAIEvaluator
from .models import (
    AnalysisRecord,
    Briefing,
    Card,
    EvalResult,
    EventRecord,
    LastRun,
    Manifest,
    RawItem,
    SourceCounts,
    YearIndex,
)
from .prefilter import load_candidate_settings
from .prune import RETENTION_DAYS, prune
from .publish import SnapshotConflict, injection, reset_stage, transaction
from .slice import run_slice, validate_snapshot
from .storage import upsert_jsonl, write_json_atomic

SCHEMAS = {
    "analysis-record": AnalysisRecord,
    "briefing": Briefing,
    "card": Card,
    "eval-result": EvalResult,
    "event-record": EventRecord,
    "manifest": Manifest,
    "raw-item": RawItem,
    "year-index": YearIndex,
}


def _collect_events(sources, now, data, config_path, *, enter=None, details=None):
    cfg = load_merge_config(config_path)
    grouped = None

    def save_events(results):
        nonlocal grouped
        if details is not None:
            details.extend(results)
        if enter:
            enter("group")
        grouped = record_events(
            (item for result in results for item in result.items),
            data,
            cfg,
            now=now,
            orgs={source.id: source.org for source in sources},
            trust={source.id: source.trust for source in sources},
            active_sources=len(sources),
            before_trends=(lambda: enter("tag")) if enter else None,
        )

    results = collect_with_health(
        sources, now, data / "state/source_health.json", before_save=save_events
    )
    return results, grouped


KST = ZoneInfo("Asia/Seoul")
MODEL_SETTINGS = {
    "model": "RADAR_MODEL",
    "daily_budget_usd": "RADAR_DAILY_BUDGET_USD",
    "input_usd_per_million": "RADAR_INPUT_USD_PER_MILLION",
    "output_usd_per_million": "RADAR_OUTPUT_USD_PER_MILLION",
}


def _run_command(arguments, data):
    started = datetime.now(timezone.utc)
    day = arguments.date or started.astimezone(KST).date()
    now = (
        started
        if day == started.astimezone(KST).date()
        else datetime.combine(day, time(7), KST)
    )
    stats = {}
    results = []
    grouped = None
    evaluator = None
    budget_before = None

    def enter(stage):
        stats["stage"] = stage
        injection(stage)

    with transaction(data) as stage:
        path = stage / "manifest.json"
        old = validate_snapshot(stage) if path.exists() else None
        published = old.published if old else None

        def run_status(outcome, *, failed_stage=None, message=None):
            return Manifest(
                published=published,
                last_success_at=old.last_success_at if old else None,
                last_run=LastRun(
                    run_id=uuid4().hex,
                    date=day,
                    started_at=started,
                    finished_at=datetime.now(timezone.utc),
                    outcome=outcome,
                    failed_stage=failed_stage,
                    message=message,
                    sources=SourceCounts(
                        active=len(results),
                        complete=sum(row.completeness == "complete" for row in results),
                        partial=sum(row.completeness == "partial" for row in results),
                        failed=sum(row.completeness == "failed" for row in results),
                    ),
                ),
            )

        if published and (
            day < published.date or (day == published.date and not arguments.force)
        ):
            manifest = run_status("skipped")
        else:
            try:
                enter("collect")
                options = {}
                if arguments.no_llm:
                    results, fetch, evaluate = demo_inputs(now)
                    options.update(
                        fetch=fetch, evaluate=evaluate, analyzer_version="demo", limit=1
                    )
                else:
                    missing = [
                        name
                        for name in (*MODEL_SETTINGS.values(), "OPENAI_API_KEY")
                        if not os.environ.get(name)
                    ]
                    if missing:
                        raise ValueError("Missing settings: " + ", ".join(missing))
                    settings = ModelConfig(
                        **{
                            key: os.environ[name]
                            for key, name in MODEL_SETTINGS.items()
                        }
                    )
                    candidates = load_candidate_settings(Path("config"))
                    evaluator = OpenAIEvaluator(
                        settings, os.environ["OPENAI_API_KEY"], data, started
                    )
                    budget_before = evaluator.budget_state()
                    results, grouped = _collect_events(
                        load_sources(arguments.config),
                        now,
                        stage,
                        arguments.merge_config,
                        enter=enter,
                        details=results,
                    )
                    options.update(
                        evaluate=evaluator,
                        analyzer_version=evaluator.version,
                        limit=candidates.pipeline.max,
                        candidate_settings=candidates,
                        events=grouped.observed,
                        emerging=grouped.emerging,
                    )
                manifest = run_slice(
                    results, now, stage, force=arguments.force, stats=stats, **options
                )
                if manifest.last_run.outcome == "ok":
                    enter("prune")
                    cutoff = (day - timedelta(days=RETENTION_DAYS)).isoformat()
                    active_references = {
                        card.event_id
                        for name in manifest.published.files
                        if name.startswith("briefings/") and Path(name).stem >= cutoff
                        for card in Briefing.model_validate_json(
                            (stage / name).read_bytes()
                        ).cards
                    }
                    prune(stage, day, protected_events=active_references)
                    enter("publish")
                    validate_snapshot(stage, references=True)
                elif manifest.last_run.failed_stage in {"collect", "group"}:
                    reset_stage(data, stage)
            except Exception as exc:
                reset_stage(data, stage)
                stage_name = stats.get("stage", "collect")
                message = (
                    str(exc)
                    if str(exc).startswith("Missing settings: ")
                    else type(exc).__name__
                )
                manifest = run_status(
                    "failed", failed_stage=stage_name, message=message
                )
                print(f"{stage_name}: {message}")

        # Keep wall-clock execution timestamps for explicit historical dates.
        manifest.last_run.started_at = started
        manifest.last_run.finished_at = datetime.now(timezone.utc)
        if manifest.last_run.outcome != "skipped":
            write_json_atomic(path, manifest.model_dump(mode="json"))
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "calls": 0,
            "estimated_usd": "0",
        }
        if budget_before is not None:
            after = evaluator.budget_state()
            usage = {
                key: getattr(after, key) - getattr(budget_before, key)
                for key in ("calls", "input_tokens", "output_tokens")
            }
            usage["estimated_usd"] = str(
                after.estimated_usd - budget_before.estimated_usd
            )
        stats.pop("stage", None)
        summary = {
            **manifest.last_run.model_dump(mode="json"),
            **usage,
            "counts": stats,
        }
        if manifest.last_run.outcome != "skipped":
            upsert_jsonl(stage / "runs" / f"{day:%Y-%m}.jsonl", [summary], "run_id")

    write_json_atomic(
        Path("out/run-log.json"),
        {
            "run": summary,
            "sources": [
                dict(
                    source_id=row.source.id,
                    completeness=row.completeness,
                    items=len(row.items),
                    requests=row.requests,
                    elapsed_ms=row.elapsed_ms,
                    partial_reasons=row.partial_reasons,
                    error=row.error,
                )
                for row in results
            ],
            "merge_log": grouped.merge_log if grouped else [],
        },
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m radar")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser(
        "export-schema", help="Export the Radar JSON Schema contracts"
    )
    export.add_argument("--output", type=Path, default=Path("schemas"))
    sources = commands.add_parser("sources")
    source_commands = sources.add_subparsers(dest="source_command", required=True)
    verify = source_commands.add_parser(
        "verify", help="Check source responses and dates"
    )
    verify.add_argument("--config", type=Path, default=Path("config/sources.yaml"))
    collect_cmd = commands.add_parser(
        "collect", help="Measure the collection window or record events without a model"
    )
    mode = collect_cmd.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--record", action="store_true")
    collect_cmd.add_argument("--config", type=Path, default=Path("config/sources.yaml"))
    collect_cmd.add_argument("--data", type=Path, default=Path("data"))
    collect_cmd.add_argument("--merge-config", type=Path, default=Path("config"))
    validate = commands.add_parser(
        "validate-data", help="Verify the briefing manifest and data"
    )
    validate.add_argument("--data", type=Path, default=Path("data"))
    run = commands.add_parser("run", help="Build and publish a briefing snapshot")
    run.add_argument("--data", type=Path)
    run.add_argument("--date", type=date.fromisoformat, help="Briefing date in Korea")
    run.add_argument("--force", action="store_true", help="Regenerate the latest date")
    run.add_argument("--config", type=Path, default=Path("config/sources.yaml"))
    run.add_argument("--merge-config", type=Path, default=Path("config"))
    run.add_argument(
        "--no-llm", action="store_true", help="Publish synthetic preview data locally"
    )
    arguments = parser.parse_args()

    if arguments.command == "run":
        data = arguments.data or Path("data")
        if arguments.no_llm and (
            arguments.data is None
            or data.resolve().is_relative_to(Path("data").resolve())
        ):
            parser.error("--no-llm requires a separate --data preview directory")
        if arguments.date and arguments.date > datetime.now(KST).date():
            parser.error("--date cannot be in the future in Korea")
        try:
            manifest = _run_command(arguments, data)
        except (SnapshotConflict, OSError, ValueError) as exc:
            parser.exit(1, f"Snapshot publication failed ({type(exc).__name__})\n")
        outcome = manifest.last_run.outcome
        if output := os.environ.get("GITHUB_OUTPUT"):
            with Path(output).open("a", encoding="utf-8") as stream:
                print(f"outcome={outcome}", file=stream)
        print(f"{manifest.last_run.date}: {outcome}")
        raise SystemExit(1 if outcome == "failed" else 0)

    if arguments.command == "validate-data":
        try:
            with transaction(arguments.data, write=False):
                validate_snapshot(arguments.data, references=True)
        except (OSError, ValueError) as exc:
            parser.exit(1, f"Snapshot validation failed ({type(exc).__name__})\n")
        print("Snapshot validated")
        return

    if arguments.command in {"sources", "collect"}:
        now = datetime.now(timezone.utc)
        sources = load_sources(arguments.config)
        if arguments.command == "collect" and arguments.record:
            with transaction(arguments.data, write=False):
                results, grouped = _collect_events(
                    sources, now, arguments.data, arguments.merge_config
                )
            print(
                f"events: {len(grouped.observed)} observed, {len(grouped.new)} new, {len(grouped.revised)} revised, {len(grouped.touched)} touched"
            )
        elif arguments.command == "collect":
            results = collect_with_health(
                sources, now, arguments.data / "state/source_health.json", dry_run=True
            )
        else:
            results = collect(sources, now)
        print(
            "source\tcompleteness\titems\tdated\tnewest_age_hours\trequests\telapsed_ms\tpartial_reasons\terror"
        )
        for result in results:
            dates = [item.published_at for item in result.items if item.published_at]
            age = f"{(now - max(dates)).total_seconds() / 3600:.1f}" if dates else "-"
            print(
                result.source.id,
                result.completeness,
                len(result.items),
                len(dates),
                age,
                result.requests,
                result.elapsed_ms,
                ",".join(result.partial_reasons) or "-",
                result.error or "-",
                sep="\t",
            )
        if not results or all(result.completeness == "failed" for result in results):
            raise SystemExit(1)
        return

    for name, model in SCHEMAS.items():
        write_json_atomic(
            arguments.output / f"{name}.schema.json", model.model_json_schema()
        )


if __name__ == "__main__":
    main()
