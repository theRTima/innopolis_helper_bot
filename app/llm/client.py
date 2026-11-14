"""OpenAI-compatible client for the local llama.cpp Gemma server."""

from __future__ import annotations

from openai import AsyncOpenAI

from app.rag.index import Chunk

SYSTEM_PROMPT = (
    "Ты корпоративный помощник первой линии поддержки. Отвечай по-русски, "
    "кратко и по делу. Используй только предоставленные фрагменты документов. "
    "Каждое утверждение подкрепляй ссылкой на номер фрагмента в квадратных "
    "скобках, например [1]. Некоторые фрагменты могут быть нерелевантны — "
    "игнорируй их. Если ответ есть хотя бы в одном фрагменте, ответь по нему, "
    "даже когда остальные фрагменты не по теме. Если ответа нет ни в одном "
    "фрагменте, прямо скажи, что в документации нет нужной информации, и не "
    "выдумывай."
)


def _format_context(results: list[tuple[float, Chunk]]) -> str:
    """Put the best chunk last: the model attends better to recent context."""

    blocks = []
    for position in range(len(results) - 1, -1, -1):
        chunk = results[position][1]
        blocks.append(
            f"[{position + 1}] {chunk.document}, стр. {chunk.page_label}\n{chunk.context}"
        )
    return "\n".join(blocks)


class LLMClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        max_tokens: int,
        temperature: float,
    ) -> None:
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature

    async def answer_question(
        self,
        question: str,
        results: list[tuple[float, Chunk]],
    ) -> str:
        user_prompt = (
            f"Вопрос: {question}\n\n"
            f"Фрагменты документации:\n\n{_format_context(results)}"
        )
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        message = response.choices[0].message
        return (
            message.content
            or getattr(message, "reasoning_content", None)
            or "Не удалось сформировать ответ."
        ).strip()
