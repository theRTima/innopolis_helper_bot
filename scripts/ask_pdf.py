#!/usr/bin/env python3
"""Search the local knowledge PDFs and ask the llama.cpp server a question."""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def extract_pdf_pages(pdf_path: Path) -> list[str]:
    result = subprocess.run(
        ["pdftotext", "-layout", pdf_path, "-"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "pdftotext не смог прочитать PDF")
    pages = [page.strip() for page in result.stdout.split("\f") if page.strip()]
    if not pages:
        raise RuntimeError("В PDF не найден текст. Возможно, это скан или документ без текстового слоя.")
    return pages


def retrieve_pages(question: str, knowledge_dir: Path, max_chars: int, top_k: int) -> list[str]:
    pdfs = sorted(knowledge_dir.glob("*.pdf"))
    if not pdfs:
        raise RuntimeError(f"В папке базы знаний нет PDF: {knowledge_dir}")

    terms = set(re.findall(r"[\w-]+", question.casefold()))
    candidates: list[tuple[int, str]] = []
    for pdf in pdfs:
        for page_number, page_text in enumerate(extract_pdf_pages(pdf), start=1):
            page_terms = set(re.findall(r"[\w-]+", page_text.casefold()))
            score = len(terms & page_terms)
            if score:
                candidates.append((score, f"Источник: {pdf.name}, стр. {page_number}\n{page_text}"))

    candidates.sort(key=lambda item: item[0], reverse=True)
    selected: list[str] = []
    chars = 0
    for _, candidate in candidates:
        if chars + len(candidate) > max_chars:
            continue
        selected.append(candidate)
        chars += len(candidate)
        if len(selected) >= top_k:
            break
    if not selected:
        raise RuntimeError("Не нашёл страниц с совпадениями по словам вопроса в PDF из data/knowledge.")
    print(f"Найдено PDF: {len(pdfs)}; в контекст добавлено страниц: {len(selected)}.", file=sys.stderr)
    return selected


def ask_server(base_url: str, model: str, question: str, passages: list[str]) -> str:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Отвечай по-русски только по приведённым фрагментам. Если доказательств недостаточно, скажи об этом. Укажи источник и номер страницы для каждого существенного факта.",
            },
            {
                "role": "user",
                "content": f"Найденные фрагменты документов базы знаний:\n\n{'\n\n---\n\n'.join(passages)}\n\nВопрос: {question}",
            },
        ],
        "temperature": 0.1,
        "max_tokens": 400,
    }
    request = Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=600) as response:
        result = json.load(response)
    return result["choices"][0]["message"]["content"].strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", help="Вопрос по документу")
    parser.add_argument("--url", default="http://127.0.0.1:8080", help="Адрес llama-server")
    parser.add_argument("--model", default="gemma-4-e2b", help="Имя модели для API")
    parser.add_argument(
        "--knowledge-dir",
        type=Path,
        default=Path("data/knowledge"),
        help="Папка с PDF базы знаний (по умолчанию: data/knowledge)",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=24000,
        help="Максимум символов PDF, отправляемых модели (по умолчанию: 24000)",
    )
    parser.add_argument("--top-k", type=int, default=6, help="Максимум подходящих страниц")
    args = parser.parse_args()

    try:
        passages = retrieve_pages(args.question, args.knowledge_dir, args.max_chars, args.top_k)
        print(ask_server(args.url, args.model, args.question, passages))
    except FileNotFoundError as error:
        print(f"Не найдена команда или файл: {error}", file=sys.stderr)
        return 1
    except (RuntimeError, HTTPError, URLError, TimeoutError, KeyError, IndexError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
