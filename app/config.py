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
    llm_session_id: str
    llm_user_agent: str
    llm_answer_reasoning_effort: str
    llm_extraction_reasoning_effort: str
    answer_max_tokens: int
    temperature: float
    top_k: int
    min_score: float
    knowledge_dir: Path
    documents_dir: Path
    database_path: Path
    database_url: str
    documents_base_url: str
    document_link_secret: str
    llm_enable_thinking: bool | None = None
    llm_thinking_budget: int | None = None
    answer_max_completion_tokens: int = 1400
    support_url: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        thinking = os.getenv("LLM_ENABLE_THINKING", "auto").lower().strip()
        if thinking not in {"auto", "true", "false"}:
            raise ValueError("LLM_ENABLE_THINKING must be auto, true or false")
        budget_text = os.getenv("LLM_THINKING_BUDGET", "").strip()
        budget = int(budget_text) if budget_text else None
        if budget is not None and not 1 <= budget <= 4096:
            raise ValueError("LLM_THINKING_BUDGET must be from 1 to 4096")
        return cls(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            llm_base_url=os.getenv("LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
            llm_model=os.getenv("LLM_MODEL", "qwen3.8-flash"),
            llm_api_key=os.getenv("LLM_API_KEY", "local"),
            llm_session_id=os.getenv(
                "LLM_SESSION_ID", "innopolis-helper-bot"
            ).strip(),
            llm_user_agent=os.getenv(
                "LLM_USER_AGENT", "innopolis-helper-bot/1.0"
            ).strip(),
            llm_answer_reasoning_effort=os.getenv(
                "LLM_ANSWER_REASONING_EFFORT", "minimal"
            ).strip(),
            llm_extraction_reasoning_effort=os.getenv(
                "LLM_EXTRACTION_REASONING_EFFORT", "low"
            ).strip(),
            answer_max_tokens=int(os.getenv("ANSWER_MAX_TOKENS", "600")),
            temperature=float(os.getenv("TEMPERATURE", "0.1")),
            top_k=int(os.getenv("TOP_K", "2")),
            min_score=float(os.getenv("MIN_SCORE", "0")),
            knowledge_dir=_path("KNOWLEDGE_DIR", "data/knowledge"),
            documents_dir=_path(
                "DOCUMENTS_DIR", "data/processed/documents"
            ),
            database_path=_path("DATABASE_PATH", "data/bot.sqlite3"),
            database_url=os.getenv("DATABASE_URL", "").strip(),
            documents_base_url=os.getenv("DOCUMENTS_BASE_URL", "").rstrip("/"),
            document_link_secret=os.getenv("DOCUMENT_LINK_SECRET", ""),
            llm_enable_thinking={"auto": None, "true": True, "false": False}[
                thinking
            ],
            llm_thinking_budget=budget,
            answer_max_completion_tokens=int(
                os.getenv("ANSWER_MAX_COMPLETION_TOKENS", "1400")
            ),
            support_url=os.getenv("SUPPORT_URL", "").strip(),
        )

    def validate(self) -> None:
        if not self.bot_token:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN не задан. Скопируйте .env.example в .env "
                "и укажите токен от @BotFather."
            )
        if bool(self.documents_base_url) != bool(self.document_link_secret):
            raise ValueError(
                "DOCUMENTS_BASE_URL и DOCUMENT_LINK_SECRET "
                "должны быть заданы вместе."
            )
