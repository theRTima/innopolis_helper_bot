"""Checkpoint 2: reproducible visual renditions of ingested sources."""

import argparse
import hashlib
import html
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageOps, __version__ as pillow_version

from .ingest import sha256_file, validate_pdf


class RenderError(Exception):
    """Rendering failed or a cached rendition is damaged."""


PIPELINE_VERSION = 1
MAX_PIXELS = 40_000_000
MAX_PAGES = 5000
OFFICE_FILTERS = {
    "docx": "writer_pdf_Export",
    "xlsx": "calc_pdf_Export",
    "pptx": "impress_pdf_Export",
}


def safe_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str):
        raise RenderError("Artifact path must be a string.")
    result = (root / relative).resolve()
    if not result.is_relative_to(root.resolve()) or result == root.resolve():
        raise RenderError(f"Artifact path escapes its directory: {relative}")
    return result


def run_office(
    command: list[str], timeout: int = 120
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RenderError(f"LibreOffice could not run: {exc}") from exc
    if result.returncode:
        raise RenderError(
            f"LibreOffice failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout)[-2000:]}"
        )
    return result


def find_office(explicit: str | None = None) -> Path:
    candidates = (
        [explicit]
        if explicit
        else [
            os.environ.get("SOFFICE_PATH"),
            shutil.which("soffice"),
            "C:/Program Files/LibreOffice/program/soffice.com",
            "C:/Program Files/LibreOffice/program/soffice.exe",
            "C:/Program Files (x86)/LibreOffice/program/soffice.com",
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        ]
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise RenderError(
        (
            "Office rendering requires LibreOffice. Install it or pass --sof"
            "fice PATH (or SOFFICE_PATH). No ready rendition has been create"
            "d."
        )
    )


def convert_office(
    source: Path, stage: Path, file_format: str, executable: Path
) -> Path:
    # An independent profile avoids interference with an open user Office
    # session.
    # Keep Office profile/input paths short: deep version folders can exceed
    # Windows path limits inside LibreOffice even when Python handles them.
    with tempfile.TemporaryDirectory(prefix="rag-office-") as temporary:
        work = Path(temporary)
        office_source = work / ("input" + source.suffix)
        shutil.copyfile(source, office_source)
        profile = work / "profile"
        profile.mkdir()
        (profile / "user").mkdir()
        (profile / "user/registrymodifications.xcu").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scri'
            'pting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3<'
            "/value></prop>"
            "</item></oor:items>",
            encoding="utf-8",
        )
        run_office(
            [
                str(executable),
                "-env:UserInstallation=" + profile.as_uri(),
                "--headless",
                "--nologo",
                "--nodefault",
                "--norestore",
                "--convert-to",
                "pdf:" + OFFICE_FILTERS[file_format],
                "--outdir",
                str(work),
                str(office_source),
            ]
        )
        converted = work / "input.pdf"
        if not converted.is_file():
            raise RenderError("LibreOffice returned without producing a PDF.")
        validate_pdf(converted)
        destination = stage / "document.pdf"
        shutil.copyfile(converted, destination)
        return destination


def render_pdf(source: Path, stage: Path, dpi: int) -> list[dict]:
    pages = []
    directory = stage / "pages"
    directory.mkdir()
    with pdfium.PdfDocument(source) as document:
        if not 0 < len(document) <= MAX_PAGES:
            raise RenderError(f"PDF page count must be 1–{MAX_PAGES}.")
        document.init_forms()
        for index in range(len(document)):
            page = document[index]
            try:
                width_pt, height_pt = page.get_size()
                scale = dpi / 72
                if (
                    math.ceil(width_pt * scale) * math.ceil(height_pt * scale)
                    > MAX_PIXELS
                ):
                    raise RenderError(
                        f"Page {index + 1} exceeds {MAX_PIXELS} pixels; "
                        "lower --dpi."
                    )
                bitmap = page.render(scale=scale)
                try:
                    image = bitmap.to_pil()
                    relative = f"pages/page_{index + 1:04d}.png"
                    image.save(stage / relative)
                    pages.append(
                        {
                            "number": index + 1,
                            "kind": "pdf_page",
                            "path": relative,
                            "width": image.width,
                            "height": image.height,
                            "width_pt": width_pt,
                            "height_pt": height_pt,
                            "rotation_degrees": page.get_rotation(),
                        }
                    )
                finally:
                    bitmap.close()
            finally:
                page.close()
    return pages


def render_image(source: Path, stage: Path) -> list[dict]:
    directory = stage / "frames"
    directory.mkdir()
    frames = []
    with Image.open(source) as original:
        count = getattr(original, "n_frames", 1)
        if count > 100:
            raise RenderError("More than 100 image frames.")
        for index in range(count):
            original.seek(index)
            image = ImageOps.exif_transpose(original.copy())
            if image.width * image.height > MAX_PIXELS:
                raise RenderError(
                    f"Frame {index + 1} exceeds {MAX_PIXELS} pixels."
                )
            image = image.convert("RGBA")
            background = Image.new("RGBA", image.size, "white")
            background.alpha_composite(image)
            relative = f"frames/frame_{index + 1:04d}.png"
            background.convert("RGB").save(stage / relative)
            frames.append(
                {
                    "number": index + 1,
                    "kind": "image_frame",
                    "path": relative,
                    "width": image.width,
                    "height": image.height,
                }
            )
    return frames


STYLE = (
    "body{font:16px system-ui;margin:24px;background:#f3f5f7;color:#182230}"
    "\nh1{font-size:24px} .grid{display:grid;grid-template-columns:repeat(au"
    "to-fit,minmax(280px,1fr));gap:20px}\nfigure{margin:0;background:white;p"
    "adding:12px;border:1px solid #d6dce4;border-radius:8px}\nimg{width:100%"
    ";height:auto}figcaption{padding:8px 0}a{color:#1557a0}\npre{margin:0;wh"
    "ite-space:pre-wrap;overflow-wrap:anywhere;tab-size:4}\n.line{display:fl"
    "ex;background:white;min-height:1.5em}.line:target{background:#fff2b2}\n"
    ".num{width:70px;flex-shrink:0;text-align:right;padding-right:16px;user-"
    "select:none;color:#65758b}\n.source{font:14px/1.5 Consolas,monospace;ov"
    "erflow:auto}.line pre{flex:1;min-width:0}"
)


def html_document(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="ru"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src'
        " 'none'; img-src 'self'; style-src 'unsafe-inline'; base-uri 'n"
        "one'; form-action 'none'\">"
        f"<title>{html.escape(title)}</title><style>{STYLE}</style><body>"
        f"<h1>{html.escape(title)}</h1>{body}</body></html>"
    )


def render_text(source: Path, stage: Path, encoding: str, title: str) -> dict:
    text = source.read_bytes().decode(encoding, errors="strict")
    lines = text.splitlines()
    if len(lines) > 200000:
        raise RenderError(
            "Text preview exceeds 200000 lines; split the input."
        )
    rows = []
    for number, line in enumerate(lines, 1):
        rows.append(
            f'<div class="line" id="L{number}">'
            f'<a class="num" href="#L{number}">{number}</a>'
            f'<pre>{html.escape(line) or " "}</pre></div>'
        )
    (stage / "index.html").write_text(
        html_document(
            title,
            (
                "<p>Source text · line anchors L1, L2, … · no pagination</p>"
                '<div class="source">'
            )
            + "\n".join(rows)
            + "</div>",
        ),
        encoding="utf-8",
    )
    return {"line_count": len(lines), "page_count": None, "frame_count": None}


def write_gallery(stage: Path, title: str, items: list[dict]) -> None:
    figures = []
    for item in items:
        path = item["path"]
        label = (
            f'{item["kind"]} {item["number"]} · '
            f'{item["width"]} × {item["height"]}'
        )
        figures.append(
            f"<figure><figcaption>{label}</figcaption>"
            f'<a href="{path}"><img loading="lazy" src="{path}" '
            f'alt="{label}"></a></figure>'
        )
    (stage / "index.html").write_text(
        html_document(
            title,
            (
                "<p>Full-size images open on click. Numbering starts at 1.</"
                'p><div class="grid">'
            )
            + "".join(figures)
            + "</div>",
        ),
        encoding="utf-8",
    )


def verify_assets(directory: Path, manifest: dict) -> None:
    assets = manifest.get("assets")
    if not isinstance(assets, list) or not assets:
        raise RenderError("Rendition has no asset inventory.")
    for asset in assets:
        path = safe_path(directory, asset["path"])
        if not path.is_file() or sha256_file(path) != asset["sha256"]:
            raise RenderError(f"Rendition asset is missing or damaged: {path}")
        if path.suffix == ".png":
            with Image.open(path) as image:
                image.verify()


def render_version(
    version_path: str | Path,
    dpi: int = 150,
    soffice: str | None = None,
    revision: str = "default",
) -> dict:
    if not 72 <= dpi <= 300:
        raise RenderError("DPI must be between 72 and 300.")
    version = Path(version_path).resolve()
    try:
        source_manifest = json.loads(
            (version / "manifest.json").read_text(encoding="utf-8")
        )
        if (
            source_manifest["schema_version"] not in (1, 2)
            or source_manifest["status"] != "uploaded"
        ):
            raise RenderError("Unsupported source manifest or upload status.")
        source = safe_path(version, source_manifest["original_path"])
        if sha256_file(source) != source_manifest["sha256"]:
            raise RenderError(
                "Original source SHA-256 does not match its manifest."
            )
        file_format = source_manifest.get("format", "pdf")
        if file_format not in {"pdf", "docx", "xlsx", "pptx", "image", "text"}:
            raise RenderError(f"Unsupported rendering format: {file_format}")
        office = (
            find_office(soffice) if file_format in OFFICE_FILTERS else None
        )
        office_version = (
            run_office([str(office), "--version"], timeout=20).stdout.strip()
            if office
            else None
        )
        configuration = {
            "pipeline_version": PIPELINE_VERSION,
            "format": file_format,
            "dpi": dpi if file_format in {"pdf", *OFFICE_FILTERS} else None,
            "pypdfium2": str(pdfium.PYPDFIUM_INFO),
            "pdfium": str(pdfium.PDFIUM_INFO),
            "pillow": pillow_version,
            "libreoffice": office_version,
            "revision": revision,
        }
        key = hashlib.sha256(
            json.dumps(configuration, sort_keys=True).encode()
        ).hexdigest()[:20]
        rendition_id = "render-" + key
        renders = version / "renders"
        renders.mkdir(exist_ok=True)
        destination = renders / rendition_id
        lock = version / ".render.lock"
        try:
            lock_stream = lock.open("x", encoding="utf-8")
        except FileExistsError as exc:
            raise RenderError(f"Rendering is locked: {lock}") from exc
        try:
            with lock_stream:
                lock_stream.write(datetime.now(timezone.utc).isoformat())
            if destination.exists():
                cached = json.loads(
                    (destination / "render_manifest.json").read_text(
                        encoding="utf-8"
                    )
                )
                if (
                    cached["status"] != "ready"
                    or cached["source_sha256"] != source_manifest["sha256"]
                    or cached["configuration"] != configuration
                ):
                    raise RenderError(
                        (
                            "Cached rendition manifest does not match the so"
                            "urce/configuration."
                        )
                    )
                verify_assets(destination, cached)
                return {
                    "cached": True,
                    "rendition_path": str(destination),
                    "manifest": cached,
                }
            with tempfile.TemporaryDirectory(
                prefix=".staging-", dir=renders
            ) as tmp:
                stage = Path(tmp)
                warnings = []
                items = []
                title = source_manifest["original_filename"]
                if file_format == "text":
                    counts = render_text(
                        source,
                        stage,
                        source_manifest["validation"]["encoding"],
                        title,
                    )
                    kind = "text_preview"
                elif file_format == "image":
                    items = render_image(source, stage)
                    counts = {
                        "page_count": None,
                        "frame_count": len(items),
                        "line_count": None,
                    }
                    kind = "image_frames"
                    warnings.append(
                        (
                            "EXIF orientation applied; transparency composit"
                            "ed on white; original unchanged."
                        )
                    )
                else:
                    pdf = (
                        convert_office(source, stage, file_format, office)
                        if office
                        else source
                    )
                    items = render_pdf(pdf, stage, dpi)
                    if (
                        file_format == "pdf"
                        and len(items) != source_manifest["page_count"]
                    ):
                        raise RenderError(
                            (
                                "Rendered page count differs from the ingest"
                                "ed PDF page count."
                            )
                        )
                    counts = {
                        "page_count": len(items),
                        "frame_count": None,
                        "line_count": None,
                    }
                    kind = "pdf_pages" if not office else "office_pdf_pages"
                    if office:
                        warnings.append(
                            (
                                "Office pagination/layout follows LibreOffic"
                                "e and installed fonts; XLSX follows print a"
                                "reas; PPTX uses static slides without notes"
                                "/animations. Inspect visually."
                            )
                        )
                if items:
                    write_gallery(stage, title, items)
                if sha256_file(source) != source_manifest["sha256"]:
                    raise RenderError("Source changed while rendering.")
                assets = [
                    {
                        "path": path.relative_to(stage).as_posix(),
                        "sha256": sha256_file(path),
                        "byte_size": path.stat().st_size,
                    }
                    for path in sorted(stage.rglob("*"))
                    if path.is_file()
                ]
                for item in items:
                    item["sha256"] = next(
                        asset["sha256"]
                        for asset in assets
                        if asset["path"] == item["path"]
                    )
                manifest = {
                    "schema_version": 1,
                    "rendition_id": rendition_id,
                    "document_id": source_manifest["document_id"],
                    "document_version": source_manifest["version_id"],
                    "source_sha256": source_manifest["sha256"],
                    "kind": kind,
                    "configuration": configuration,
                    "status": "ready",
                    "visual_review": "pending",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    **counts,
                    "preview_path": "index.html",
                    "items": items,
                    "assets": assets,
                    "warnings": warnings,
                }
                verify_assets(stage, manifest)
                (stage / "render_manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                stage.rename(destination)
                return {
                    "cached": False,
                    "rendition_path": str(destination),
                    "manifest": manifest,
                }
        finally:
            lock.unlink()
    except RenderError:
        raise
    except Exception as exc:
        raise RenderError(f"Rendering failed: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Checkpoint 2: render a stored document version"
    )
    parser.add_argument(
        "version_path",
        type=Path,
        nargs="?",
        help="Version directory containing manifest.json and original.*",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Render every stored version and create a batch report/index",
    )
    parser.add_argument(
        "--storage-root", type=Path, default=Path("data/processed/documents")
    )
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument(
        "--soffice", help="Path to LibreOffice soffice.com/executable"
    )
    parser.add_argument(
        "--revision",
        default="default",
        help=(
            "Change after fonts/environment change to produce a separate ren"
            "dition"
        ),
    )
    args = parser.parse_args()
    if args.all == bool(args.version_path):
        parser.error("Provide either version_path or --all.")
    if args.all:
        try:
            report = render_all(
                args.storage_root, args.dpi, args.soffice, args.revision
            )
        except (RenderError, OSError) as exc:
            print(f"Render failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 0 if report["status"] == "ready" else 1
    try:
        result = render_version(
            args.version_path, args.dpi, args.soffice, args.revision
        )
    except RenderError as exc:
        print(f"Render failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "cached": result["cached"],
                "rendition_path": result["rendition_path"],
                "page_count": result["manifest"]["page_count"],
                "frame_count": result["manifest"]["frame_count"],
                "line_count": result["manifest"]["line_count"],
                "preview_path": str(
                    Path(result["rendition_path"]) / "index.html"
                ),
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0


def render_all(
    storage_root: str | Path,
    dpi: int = 150,
    soffice: str | None = None,
    revision: str = "default",
) -> dict:
    root = Path(storage_root).resolve()
    versions = sorted(root.glob("*/*/manifest.json"))
    if not versions:
        raise RenderError(f"No stored document versions found in {root}.")
    results = []
    links = []
    for source_manifest in versions:
        try:
            result = render_version(
                source_manifest.parent, dpi, soffice, revision
            )
            manifest = result["manifest"]
            directory = Path(result["rendition_path"])
            preview = directory / "index.html"
            results.append(
                {
                    "status": "ready",
                    "document_id": manifest["document_id"],
                    "document_version": manifest["document_version"],
                    "cached": result["cached"],
                    "page_count": manifest["page_count"],
                    "frame_count": manifest["frame_count"],
                    "line_count": manifest["line_count"],
                    "rendition_path": str(directory),
                }
            )
            name = html.escape(manifest["document_id"])
            link = preview.relative_to(root).as_posix()
            links.append(
                f'<li><a href="{link}">{name}</a> · '
                f'{html.escape(manifest["kind"])}</li>'
            )
        except RenderError as exc:
            results.append(
                {
                    "status": "failed",
                    "version_path": str(source_manifest.parent),
                    "error": str(exc),
                }
            )
            links.append(
                f"<li>Failed: {html.escape(str(source_manifest.parent))}: "
                f"{html.escape(str(exc))}</li>"
            )
    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": (
            "ready"
            if all(item["status"] == "ready" for item in results)
            else "partial_failure"
        ),
        "versions_checked": len(results),
        "total_pdf_pages": sum(
            item.get("page_count") or 0 for item in results
        ),
        "results": results,
    }
    (root / "render-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (root / "render-index.html").write_text(
        html_document(
            "Результаты рендеринга", "<ul>" + "".join(links) + "</ul>"
        ),
        encoding="utf-8",
    )
    return report


if __name__ == "__main__":
    raise SystemExit(main())
