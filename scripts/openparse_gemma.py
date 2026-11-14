#!/usr/bin/env python3
"""Parse a document with open-parse and ask the local Gemma server about it."""

import argparse
import base64
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SYSTEM_PROMPT = (
    "Ты корпоративный помощник. Отвечай по-русски, кратко и по делу. "
    "Используй только предоставленный текст документа и изображения страниц. "
    "Если ответа в документе нет, прямо скажи об этом и не выдумывай. "
    "По возможности указывай номер страницы."
)


def parse_document(path: str, max_chars: int) -> tuple[str, list[dict]]:
    import openparse

    doc = openparse.DocumentParser().parse(path)
    blocks: list[str] = []
    images: list[dict] = []

    for node in doc.nodes:
        pages = (
            str(node.start_page)
            if node.start_page == node.end_page
            else f"{node.start_page}-{node.end_page}"
        )
        text = node.text.strip()
        if text:
            blocks.append(f"[стр. {pages}]\n{text}")
        for element in node.elements:
            if type(element).__name__ == "ImageElement":
                images.append(
                    {
                        "page": element.page,
                        "mimetype": element.image_mimetype,
                        "data": element.image,
                    }
                )

    context = "\n\n".join(blocks)
    if max_chars and len(context) > max_chars:
        print(
            f"Предупреждение: текст сокращён с {len(context)} до {max_chars} символов.",
            file=sys.stderr,
        )
        context = context[:max_chars]
    return context, images


def render_page(path: str, page: int, dpi: int) -> dict:
    import pymupdf

    with pymupdf.open(path) as doc:
        pixmap = doc[page - 1].get_pixmap(dpi=dpi)
        return {
            "page": page,
            "mimetype": "image/png",
            "data": base64.b64encode(pixmap.tobytes("png")).decode("ascii"),
        }


def build_content(question: str, context: str, images: list[dict]) -> list[dict]:
    parts: list[dict] = []
    text = f"Вопрос: {question}"
    if context:
        text += f"\n\nТекст документа (open-parse):\n\n{context}"
    parts.append({"type": "text", "text": text})
    for image in images:
        parts.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image['mimetype']};base64,{image['data']}"
                },
            }
        )
    return parts


def ask(
    base_url: str,
    model: str,
    content: list[dict],
    max_tokens: int,
    temperature: float,
) -> tuple[str, str, dict]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    request = Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=900) as response:
        result = json.load(response)
    if "error" in result:
        raise RuntimeError(result["error"].get("message", result["error"]))
    message = result["choices"][0]["message"]
    return (
        (message.get("content") or "").strip(),
        (message.get("reasoning_content") or "").strip(),
        result.get("usage", {}),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", help="Путь к PDF")
    parser.add_argument("question", help="Вопрос по документу")
    parser.add_argument("--url", default="http://127.0.0.1:8080", help="Адрес llama-server")
    parser.add_argument("--model", default="gemma-4-e2b", help="Имя модели для API")
    parser.add_argument(
        "--max-chars",
        type=int,
        default=12000,
        help="Максимум символов текста open-parse (по умолчанию: 12000)",
    )
    parser.add_argument("--max-tokens", type=int, default=700)
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument(
        "--page",
        type=int,
        action="append",
        default=[],
        help="Отрендерить страницу в PNG и отправить в Gemma (можно повторять)",
    )
    parser.add_argument("--page-dpi", type=int, default=200)
    parser.add_argument(
        "--extract-images",
        action="store_true",
        help="Отправить изображения, которые open-parse извлёк из PDF",
    )
    parser.add_argument(
        "--no-text",
        action="store_true",
        help="Не отправлять текстовый контекст, только изображения",
    )
    parser.add_argument(
        "--show-context",
        action="store_true",
        help="Напечатать извлечённый текст перед ответом",
    )
    args = parser.parse_args()

    try:
        context, extracted_images = parse_document(
            args.file, 0 if args.no_text else args.max_chars
        )
        images: list[dict] = []
        for page in args.page:
            images.append(render_page(args.file, page, args.page_dpi))
        if args.extract_images:
            images.extend(extracted_images)

        if args.show_context:
            print(context, file=sys.stderr)
        if images:
            print(
                f"Изображений к отправке: {len(images)} "
                f"(стр. {', '.join(str(i['page']) for i in images)})",
                file=sys.stderr,
            )

        content = build_content(args.question, context, images)
        answer, reasoning, usage = ask(
            args.url,
            args.model,
            content,
            args.max_tokens,
            args.temperature,
        )
        print(answer or reasoning)
        if usage:
            print(f"\n[tokens: {usage}]", file=sys.stderr)
    except FileNotFoundError as error:
        print(f"Не найдена команда или файл: {error}", file=sys.stderr)
        return 1
    except (RuntimeError, HTTPError, URLError, TimeoutError, KeyError, IndexError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
