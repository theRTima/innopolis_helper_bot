"""Format adapters for validation; no content extraction or rendering."""

import codecs
import csv
import io
import json
import warnings
import zipfile
from pathlib import Path
from xml.etree import ElementTree


class IngestionError(Exception):
    """An input, storage, or integrity error understandable by a CLI user."""


TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".rst",
    ".adoc",
    ".html",
    ".htm",
    ".json",
    ".jsonl",
    ".xml",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".csv",
    ".tsv",
    ".log",
    ".sql",
    ".graphql",
    ".proto",
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".kt",
    ".kts",
    ".cs",
    ".go",
    ".rs",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".swift",
    ".rb",
    ".php",
    ".sh",
    ".bash",
    ".ps1",
    ".bat",
    ".cmd",
    ".css",
    ".scss",
    ".vue",
    ".svelte",
    ".ipynb",
    ".tex",
    ".properties",
    ".gradle",
}
TEXT_NAMES = {
    "readme",
    "license",
    "dockerfile",
    "makefile",
    "cmakelists.txt",
    ".gitignore",
    ".dockerignore",
    ".editorconfig",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
OFFICE_TYPES = {
    ".docx": "word",
    ".xlsx": "spreadsheet",
    ".pptx": "presentation",
}


def detect_format(source: Path, format_override: str | None = None) -> str:
    if format_override:
        return format_override
    suffix = source.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in OFFICE_TYPES:
        return suffix[1:]
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in TEXT_EXTENSIONS or source.name.lower() in TEXT_NAMES:
        return "text"
    raise IngestionError(
        f"Unsupported extension '{suffix or '(none)'}'. "
        "Supported: PDF, DOCX, XLSX, PPTX, "
        "PNG/JPEG/WebP/TIFF/BMP and text/code. For other text files use "
        "--format text. "
        "Convert legacy DOC/XLS/PPT or extract archives separately."
    )


def validate_xml(data: bytes):
    # Do not accept DTDs/entities in uploaded XML, including Office package
    # XML.
    upper = data.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise IngestionError(
            "XML DTDs and entity declarations are not supported."
        )
    return ElementTree.fromstring(data)


def validate_office(path: Path, file_format: str) -> dict:
    required = {
        "docx": "word/document.xml",
        "xlsx": "xl/workbook.xml",
        "pptx": "ppt/presentation.xml",
    }
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if (
            len(entries) > 10000
            or sum(item.file_size for item in entries) > 200 * 1024 * 1024
        ):
            raise IngestionError(
                "Office package exceeds 10000 entries or 200 MiB uncompressed."
            )
        names = [item.filename for item in entries]
        if len(names) != len(set(names)):
            raise IngestionError(
                "Office package contains duplicate member names."
            )
        for name in (
            "[Content_Types].xml",
            "_rels/.rels",
            required[file_format],
        ):
            if name not in names:
                raise IngestionError(
                    f"Invalid {file_format.upper()} package: missing {name}."
                )
        if any(name.lower().endswith("vbaproject.bin") for name in names):
            raise IngestionError(
                "Macro-enabled Office packages are not supported."
            )
        # Read all members: checks ZIP CRCs and detects damaged embedded
        # assets.
        for item in entries:
            data = archive.read(item)
            if item.filename.endswith((".xml", ".rels")):
                validate_xml(data)
    if file_format == "docx":
        from docx import Document

        document = Document(path)
        return {
            "paragraph_count": len(document.paragraphs),
            "table_count": len(document.tables),
            "section_count": len(document.sections),
        }
    if file_format == "xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(
            path, read_only=True, data_only=False, keep_links=False
        )
        try:
            return {
                "sheet_count": len(workbook.sheetnames),
                "sheet_names": workbook.sheetnames,
            }
        finally:
            workbook.close()
    from pptx import Presentation

    presentation = Presentation(path)
    return {"slide_count": len(presentation.slides)}


def validate_text(path: Path, source: Path, encoding: str | None) -> dict:
    raw = path.read_bytes()
    if not raw:
        raise IngestionError("Text file is empty.")
    if encoding is None:
        if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
            encoding = "utf-32"
        elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            encoding = "utf-16"
        else:
            encoding = "utf-8-sig"
    try:
        text = raw.decode(encoding, errors="strict")
    except (UnicodeError, LookupError) as exc:
        raise IngestionError(
            (
                "Text decoding failed. Use --encoding for a known encoding, "
                "e.g. cp1251."
            )
        ) from exc
    if not text.strip() or any(
        ord(char) < 32 and char not in "\t\r\n\f" for char in text
    ):
        raise IngestionError(
            "File is blank or contains binary control characters."
        )
    suffix = source.suffix.lower()
    if suffix in {".json", ".ipynb"}:
        json.loads(text)
    elif suffix == ".jsonl":
        for line in text.splitlines():
            if line.strip():
                json.loads(line)
    elif suffix == ".xml":
        validate_xml(text.encode("utf-8"))
    elif suffix == ".toml":
        import tomllib

        tomllib.loads(text)
    details = {
        "encoding": encoding,
        "line_count": len(text.splitlines()),
        "character_count": len(text),
    }
    if suffix in {".csv", ".tsv"}:
        rows = csv.reader(
            io.StringIO(text, newline=""),
            delimiter="\t" if suffix == ".tsv" else ",",
            strict=True,
        )
        details["row_count"] = sum(1 for _ in rows)
    return details


def validate_image(path: Path, source: Path) -> dict:
    from PIL import Image

    expected = {
        ".png": "PNG",
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".webp": "WEBP",
        ".tif": "TIFF",
        ".tiff": "TIFF",
        ".bmp": "BMP",
    }
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as image:
            if (
                source.suffix.lower() in expected
                and image.format != expected[source.suffix.lower()]
            ):
                raise IngestionError(
                    "Image contents do not match the extension."
                )
            details = {
                "image_format": image.format,
                "width": image.width,
                "height": image.height,
                "frame_count": getattr(image, "n_frames", 1),
            }
            if details["frame_count"] > 100:
                raise IngestionError(
                    "Images with more than 100 frames are not supported."
                )
            image.verify()
        with Image.open(path) as image:
            for index in range(details["frame_count"]):
                image.seek(index)
                image.load()
    return details


def validate_source(
    path: Path, source: Path, file_format: str, encoding: str | None
) -> dict:
    try:
        if encoding and file_format != "text":
            raise IngestionError(
                "--encoding applies only to text/code inputs."
            )
        if file_format == "pdf":
            from .ingest import validate_pdf

            return {
                "source_kind": "pdf",
                "page_count": validate_pdf(path),
                "validation": {},
            }
        if file_format in {"docx", "xlsx", "pptx"}:
            details = validate_office(path, file_format)
            kind = OFFICE_TYPES["." + file_format]
        elif file_format == "text":
            details = validate_text(path, source, encoding)
            kind = "text"
        elif file_format == "image":
            details = validate_image(path, source)
            kind = "image"
        else:
            raise IngestionError(f"Unsupported format: {file_format}")
        return {"source_kind": kind, "page_count": None, "validation": details}
    except IngestionError:
        raise
    except Exception as exc:
        raise IngestionError(
            f"{file_format.upper()} validation failed: {exc}"
        ) from exc
