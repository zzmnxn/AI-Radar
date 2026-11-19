import sys

from radar.__main__ import main
from radar.collect import CollectResult


def test_verify_reports_source_results(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["radar", "sources", "verify"])
    monkeypatch.setattr(
        "radar.__main__.collect",
        lambda sources, now: [CollectResult(source, "partial") for source in sources],
    )
    main()
    output = capsys.readouterr().out
    assert "hackernews-front-page" in output
    assert all(row.split("\t")[1] == "partial" for row in output.splitlines()[1:])
