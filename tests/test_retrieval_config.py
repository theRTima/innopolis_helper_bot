import pytest

from app.config import Settings


def test_default_seed_count_and_explicit_override(monkeypatch):
    monkeypatch.delenv("TOP_K", raising=False)
    assert Settings.from_env().top_k == 2
    monkeypatch.setenv("TOP_K", "6")
    assert Settings.from_env().top_k == 6


def test_extraction_concurrency_defaults_and_override(monkeypatch):
    for name in (
        "EXTRACTION_VISION_CONCURRENCY",
        "EXTRACTION_TEXT_CONCURRENCY",
        "EXTRACTION_MIN_IMAGE_PIXELS",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert settings.extraction_vision_concurrency == 2
    assert settings.extraction_text_concurrency == 4
    assert settings.extraction_min_image_pixels == 1024

    monkeypatch.setenv("EXTRACTION_VISION_CONCURRENCY", "4")
    monkeypatch.setenv("EXTRACTION_MIN_IMAGE_PIXELS", "0")
    settings = Settings.from_env()
    assert settings.extraction_vision_concurrency == 4
    assert settings.extraction_min_image_pixels == 0


def test_invalid_extraction_concurrency_is_rejected(monkeypatch):
    monkeypatch.setenv("EXTRACTION_VISION_CONCURRENCY", "0")
    with pytest.raises(ValueError, match="EXTRACTION_VISION_CONCURRENCY"):
        Settings.from_env()
