"""Search-only lexical hints; never source evidence for an answer."""

import re

VISUAL_HINTS = {
    "diagram": "схема диаграмма блок-схема ветвление переход",
    "image": "изображение картинка",
    "screenshot": "скриншот экран интерфейс",
    "form": "форма поля интерфейс",
    "table": "таблица строки столбцы",
}
TRANSLATIONS = {
    "paperclip": "скрепка вложение",
    "icon": "значок иконка",
    "button": "кнопка",
    "filename": "имя файла вложение",
    "flowchart": "схема бизнес-процесса",
}
STOP_WORDS = set(
    "мне необходимо узнать какие какой как кто где когда почему это "
    "для по на в из у и с со не его него ли быть могут опиши пожалуйста "
    "основные порядку теперь расскажи про о об что он она мы вы".split()
)


def is_navigation(chunk):
    title = chunk.title.casefold().strip()
    # A section named "Contents" may also contain actual instructions.
    prose = [
        line
        for line in (chunk.content or chunk.text).splitlines()
        if len(line) > 100 and not re.search(r"\.{4,}", line)
    ]
    if prose:
        return False
    return (
        "table of contents" in title
        or title in {"содержание", "оглавление", "1 содержание"}
        or len(re.findall(r"\.{4,}\s*\d+", chunk.text)) >= 2
    )


def search_representation(chunk):
    text = chunk.content or chunk.text
    hints = [VISUAL_HINTS.get(chunk.chunk_type, "")]
    if chunk.title and chunk.title.casefold() not in chunk.text.casefold():
        hints.append(chunk.title)
    for english, russian in TRANSLATIONS.items():
        if re.search(r"\b" + english + r"s?\b", text, re.IGNORECASE):
            hints.append(russian)
    if (
        chunk.chunk_type == "diagram"
        and chunk.data
        and any(
            node.get("kind") == "decision"
            for node in chunk.data.get("nodes", [])
            if isinstance(node, dict)
        )
    ):
        hints.append("условие ветвление")
    # Structured content is already flattened in chunk.text. Do not duplicate
    # it or copy domain answers/benchmark expectations into search metadata.
    return "\n".join([chunk.text, *hints])


def query_representation(query):
    tokens = re.findall(r"[a-zа-яё0-9]+", query.lower())
    tokens = [token for token in tokens if token not in STOP_WORDS]
    if any(
        token.startswith("согласов") or token.startswith("согласующ")
        for token in tokens
    ):
        tokens.append("согласование")
    if any(
        token.startswith("график") or token.startswith("расписан")
        for token in tokens
    ):
        tokens.extend(["дежурство", "время"])
    if any(token.startswith("заявк") for token in tokens):
        tokens.append("запрос")
    return " ".join(dict.fromkeys(tokens))
