"""Minimal configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _path(name: str, default: str) -> Path:
    return Path(os.getenv(name, default)).expanduser()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    answer_max_tokens: int
    temperature: float
    top_k: int
    min_score: float
    knowledge_dir: Path
    chunks_dir: Path
    documents_dir: Path
    database_path: Path
    database_url: str
    documents_base_url: str
    document_link_secret: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            llm_base_url=os.getenv("LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
            llm_model=os.getenv(
                "LLM_MODEL", "mlx-community/gemma-4-e2b-it-4bit"
            ),
            llm_api_key=os.getenv("LLM_API_KEY", "local"),
            answer_max_tokens=int(os.getenv("ANSWER_MAX_TOKENS", "600")),
            temperature=float(os.getenv("TEMPERATURE", "0.1")),
            top_k=int(os.getenv("TOP_K", "2")),
            min_score=float(os.getenv("MIN_SCORE", "0")),
            knowledge_dir=_path("KNOWLEDGE_DIR", "data/knowledge"),
            chunks_dir=_path("CHUNKS_DIR", "data/processed"),
            documents_dir=_path(
                "DOCUMENTS_DIR", "data/processed/documents"
            ),
            database_path=_path("DATABASE_PATH", "data/bot.sqlite3"),
            database_url=os.getenv("DATABASE_URL", "").strip(),
            documents_base_url=os.getenv("DOCUMENTS_BASE_URL", "").rstrip("/"),
            document_link_secret=os.getenv("DOCUMENT_LINK_SECRET", ""),
        )

    def validate(self) -> None:
        if not self.bot_token:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN не задан. Скопируйте .env.example в .env "
                "и укажите токен от @BotFather."
            )
        if bool(self.documents_base_url) != bool(self.document_link_secret):
            raise ValueError(
                "DOCUMENTS_BASE_URL и DOCUMENT_LINK_SECRET должны быть заданы вместе."
            )
