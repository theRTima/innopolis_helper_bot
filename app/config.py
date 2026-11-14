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
    database_path: Path

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            llm_base_url=os.getenv("LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
            llm_model=os.getenv("LLM_MODEL", "gemma-4-e2b"),
            llm_api_key=os.getenv("LLM_API_KEY", "local"),
            answer_max_tokens=int(os.getenv("ANSWER_MAX_TOKENS", "600")),
            temperature=float(os.getenv("TEMPERATURE", "0.1")),
            top_k=int(os.getenv("TOP_K", "2")),
            min_score=float(os.getenv("MIN_SCORE", "0")),
            knowledge_dir=_path("KNOWLEDGE_DIR", "data/knowledge"),
            database_path=_path("DATABASE_PATH", "storage/bot.db"),
        )

    def validate(self) -> None:
        if not self.bot_token:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN не задан. Скопируйте .env.example в .env "
                "и укажите токен от @BotFather."
            )
