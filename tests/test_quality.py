from radar.quality import summarize


def test_quality_unknown_does_not_count_as_miss_or_match():
    expected = [{"topic": "new", "event_id": "e"}, {"topic": "unresolved"}]
    report = summarize(expected, {"e"}, {"e"}, set())
    assert report["candidate"] == {"hits": 1, "known": 1, "unknown": 1}
    assert report["published"] == {"hits": 0, "known": 1, "unknown": 1}
    assert report["cases"][0]["stage"] == "candidate"
