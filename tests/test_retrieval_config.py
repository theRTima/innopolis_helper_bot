from app.config import Settings


def test_default_seed_count_and_explicit_override(monkeypatch):
    monkeypatch.delenv("TOP_K", raising=False)
    assert Settings.from_env().top_k == 2
    monkeypatch.setenv("TOP_K", "6")
    assert Settings.from_env().top_k == 6
