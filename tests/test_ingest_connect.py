import psycopg
import pytest

from radar import ingest_store


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("password authentication failed for user x", "auth"),
        ("SSL error: certificate verify failed", "tls"),
        ("could not translate host name", "dns"),
        ("connection timeout expired", "timeout"),
        ("something else entirely", "other"),
    ],
)
def test_connect_failure_names_a_category_without_leaking_the_dsn(
    monkeypatch, capsys, text, category
):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:secret@db.example.com/x")

    def boom(*args, **kwargs):
        raise psycopg.OperationalError(text + " secret db.example.com")

    monkeypatch.setattr(psycopg, "connect", boom)
    with pytest.raises(psycopg.OperationalError):
        ingest_store.connect()
    err = capsys.readouterr().err
    assert f"ingest-db connect failed: {category}" in err
    assert "secret" not in err and "db.example.com" not in err
