"""Compare a dated run with explicitly matched briefing events."""

import argparse
import json
from pathlib import Path


def summarize(cases, collected, candidates, published):
    totals = {
        key: {"hits": 0, "known": 0, "unknown": 0}
        for key in ("collected", "candidate", "published")
    }
    rows = []
    cases = [case for case in cases if case.get("preference", "liked") == "liked"]
    for case in cases:
        event = case.get("event_id")
        stage = "unknown"
        for key, reached in (
            ("collected", collected),
            ("candidate", candidates),
            ("published", published),
        ):
            counts = totals[key]
            if event is None or reached is None:
                counts["unknown"] += 1
                continue
            counts["known"] += 1
            counts["hits"] += event in reached
            if event in reached:
                stage = key
            elif key == "collected":
                stage = "missed"
        rows.append({"topic": case["topic"], "stage": stage})
    return {
        **totals,
        "cases": rows,
        "radar_only_for_review": sorted(
            (published or set()) - {c.get("event_id") for c in cases}
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("log", type=Path)
    parser.add_argument("data", type=Path)
    args = parser.parse_args()
    reference = json.loads(args.reference.read_text())
    run = json.loads(args.log.read_text())["run"]
    day = run["date"]
    if not reference["window"][0] <= day <= reference["window"][1]:
        parser.error("Run date outside reference window")
    path = args.data / "briefings" / f"{day}.json"
    published = None
    if run["outcome"] == "ok" and path.exists():
        published = {c["event_id"] for c in json.loads(path.read_text())["cards"]}
    result = summarize(
        reference["cases"], run.get("observed_ids"), run.get("candidate_ids"), published
    )
    print(
        json.dumps(
            {
                "date": day,
                "usage": {
                    k: run.get(k)
                    for k in ("calls", "estimated_usd", "input_tokens", "output_tokens")
                },
                **result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
