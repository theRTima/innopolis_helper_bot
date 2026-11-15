"""Checkpoint 3: turn a stored document version into retrieval chunks.

The source of truth is the immutable version from ``rag_pipeline.ingest``.
Rendering is optional and is used for Office documents and for visual
fallbacks.  Chunks are written next to the version with their own manifest and
cache key, so extraction can be rerun safely.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.ingestion import new_pipeline
from app.ingestion import normalized as norm
from app.ingestion import vision_parser as vp
from app.ingestion.assets import verify_assets
from app.ingestion.quality import coverage_report
from rag_pipeline.ingest import sha256_file
from rag_pipeline.render import RenderError

PIPELINE_VERSION = 3
MAX_TOKENS_TARGET = 600
SUPPORTED_FORMATS = {"pdf", "docx", "xlsx", "pptx", "image", "text"}
OFFICE_FORMATS = {"docx", "xlsx", "pptx"}


class ExtractionError(Exception):
    """Extraction failed or a cached rendition is damaged."""


def _version(value: str) -> str:
    try:
        return importlib.metadata.version(value)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def prompt_hashes() -> dict[str, str]:
    prompts = {
        "image_analysis": vp.PROMPTS_DIR / "image_analysis.j2",
        "vision_chunker": vp.PROMPTS_DIR / "vision_chunker.j2",
        "markdown_prompt": vp.PROMPTS_DIR / "markdown_prompt.j2",
    }
    return {
        name: sha256_file(path) if path.is_file() else "missing"
        for name, path in prompts.items()
    }


def configuration() -> dict:
    return {
        "pipeline_version": PIPELINE_VERSION,
        "parser": "docling+vision-normalized",
        "docling": _version("docling"),
        "openai": _version("openai"),
        "pymupdf": _version("pymupdf"),
        "provider": vp.SETTINGS.llm_base_url,
        "model": vp.MODEL_ID,
        "dpi": vp.DPI,
        "max_tokens": vp.MAX_TOKENS,
        "prompts": prompt_hashes(),
        "code": {
            "new_pipeline": sha256_file(Path(new_pipeline.__file__)),
            "normalized": sha256_file(Path(norm.__file__)),
        },
    }


def latest_render(
    version: Path, source_sha256: str, file_format: str
) -> tuple[Path, dict]:
    if file_format not in OFFICE_FORMATS:
        raise ExtractionError("Office format expected.")
    renders = version / "renders"
    candidates: list[tuple[str, Path, dict]] = []
    for manifest_path in renders.glob("render-*/render_manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            manifest.get("status") != "ready"
            or manifest.get("source_sha256") != source_sha256
        ):
            continue
        pdf = manifest_path.parent / "document.pdf"
        if not pdf.is_file():
            continue
        candidates.append(
            (
                str(manifest.get("created_at", "")),
                manifest_path.parent,
                manifest,
            )
        )
    if not candidates:
        raise ExtractionError(
            "No ready render exists for this Office version. "
            "Run: python -m rag_pipeline.render <version_path>"
        )
    _created, directory, manifest = max(candidates, key=lambda item: item[0])
    return directory, manifest


def choose_artifact(version: Path, manifest: dict) -> tuple[Path, str, str]:
    """Return artifact path, format and a stable artifact kind."""
    file_format = manifest.get("format", "pdf")
    if file_format not in SUPPORTED_FORMATS:
        raise ExtractionError(f"Unsupported extraction format: {file_format}")
    if file_format in OFFICE_FORMATS:
        render_dir, _render_manifest = latest_render(
            version, manifest["sha256"], file_format
        )
        return render_dir / "document.pdf", file_format, "rendered-pdf"
    source = version / manifest["original_path"]
    return source, file_format, "original"


def _write_markdown(chunks: list[dict], path: Path) -> None:
    sections = []
    for chunk in chunks:
        header = f"### [{chunk['type']}] page {chunk['page']}"
        if chunk.get("page_to") and chunk["page_to"] != chunk["page"]:
            header += f"-{chunk['page_to']}"
        if chunk.get("title"):
            header += f" — {chunk['title']}"
        sections.append(f"{header}\n\n{chunk['content']}")
    path.write_text("\n\n".join(sections) + "\n", encoding="utf-8")


def _decode_text(path: Path, manifest: dict) -> str:
    validation = manifest.get("validation") or {}
    encoding = validation.get("encoding") or "utf-8-sig"
    return path.read_bytes().decode(encoding, errors="strict")


def extract_version(
    version_path: str | Path,
    *,
    target_tokens: int = MAX_TOKENS_TARGET,
    pages_spec: str | None = None,
    review_markdown: bool = False,
) -> dict:
    version = Path(version_path).resolve()
    try:
        manifest = json.loads(
            (version / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise ExtractionError(f"Cannot read version manifest: {exc}") from exc

    if manifest.get("schema_version") not in (1, 2):
        raise ExtractionError("Unsupported source manifest.")
    if manifest.get("status") != "uploaded":
        raise ExtractionError("Source version is not in uploaded state.")

    original = version / manifest["original_path"]
    if not original.is_file():
        raise ExtractionError("Stored original is missing.")
    source_sha256 = sha256_file(original)
    if source_sha256 != manifest["sha256"]:
        raise ExtractionError(
            "Stored original SHA-256 does not match manifest."
        )

    artifact, file_format, artifact_kind = choose_artifact(version, manifest)
    if not artifact.is_file():
        raise ExtractionError(f"Extraction artifact is missing: {artifact}")
    artifact_rel = artifact.relative_to(version).as_posix()
    artifact_sha256 = sha256_file(artifact)

    config = {
        **configuration(),
        "target_tokens": target_tokens,
        "format": file_format,
        "artifact_kind": artifact_kind,
        "artifact_sha256": artifact_sha256,
        "pages": pages_spec or "all",
    }
    key = hashlib.sha256(
        json.dumps(config, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]
    chunk_id = "chunk-" + key
    chunk_root = version / "chunks"
    chunk_root.mkdir(exist_ok=True)
    destination = chunk_root / chunk_id
    lock = version / ".extract.lock"

    try:
        lock_stream = lock.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise ExtractionError(f"Extraction is locked: {lock}") from exc

    try:
        with lock_stream:
            lock_stream.write(datetime.now(timezone.utc).isoformat())

        if destination.exists():
            cached = json.loads(
                (destination / "chunk_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            if (
                cached.get("status") not in {"ready", "partial"}
                or cached.get("source_sha256") != source_sha256
                or cached.get("configuration") != config
            ):
                raise ExtractionError(
                    "Cached chunk manifest does not match "
                    "source/configuration."
                )
            chunks_file = destination / "chunks.jsonl"
            if not chunks_file.is_file() or sha256_file(
                chunks_file
            ) != cached.get("chunks_sha256"):
                raise ExtractionError("Cached chunks are damaged.")
            verify_assets(destination, cached.get("assets", []))
            if (
                cached.get("normalized_sha256")
                and sha256_file(destination / "normalized.json")
                != cached["normalized_sha256"]
            ):
                raise ExtractionError("Normalized evidence checksum mismatch")
            return {
                "cached": True,
                "chunk_path": str(destination),
                "manifest": cached,
            }

        chunks: list[dict] = []
        warnings: list[str] = []
        with tempfile.TemporaryDirectory(
            prefix=".staging-", dir=chunk_root
        ) as tmp:
            stage = Path(tmp)
            temp_images = stage / "images"
            temp_images.mkdir()

            if file_format == "text":
                text = _decode_text(artifact, manifest)
                chunks = new_pipeline.build_from_text(
                    text,
                    document_id=manifest["document_id"],
                    version_id=manifest["version_id"],
                    source_name=manifest["original_filename"],
                    source_sha256=source_sha256,
                    artifact=artifact_rel,
                )
                for chunk in chunks:
                    chunk["artifact_sha256"] = artifact_sha256
            else:
                if file_format == "image":
                    nd, build_warnings = new_pipeline.build_from_image(
                        artifact,
                        document_id=manifest["document_id"],
                        version_id=manifest["version_id"],
                        temp_dir=temp_images,
                    )
                else:
                    nd, build_warnings = new_pipeline.build_from_pdf(
                        artifact,
                        document_id=manifest["document_id"],
                        version_id=manifest["version_id"],
                        temp_dir=temp_images,
                        pages_spec=pages_spec,
                    )
                warnings.extend(build_warnings)
                raw_chunks, chunk_warnings = new_pipeline.chunk_elements(
                    nd.prompt_elements(),
                    document_id=manifest["document_id"],
                    version_id=manifest["version_id"],
                    target_tokens=target_tokens,
                )
                warnings.extend(chunk_warnings)
                chunks = nd.resolve_chunks(
                    raw_chunks,
                    source_name=manifest["original_filename"],
                    document_id=manifest["document_id"],
                    version_id=manifest["version_id"],
                    source_sha256=source_sha256,
                    artifact=artifact_rel,
                    artifact_sha256=artifact_sha256,
                )
                if not chunks:
                    warnings.append(
                        "model chunker produced no usable chunks; "
                        "used deterministic fallback"
                    )
                    chunks = nd.fallback_chunks(
                        source_name=manifest["original_filename"],
                        document_id=manifest["document_id"],
                        version_id=manifest["version_id"],
                        source_sha256=source_sha256,
                        artifact=artifact_rel,
                        artifact_sha256=artifact_sha256,
                    )
                warnings.extend(nd.warnings)

            chunks = norm.finalize_chunks(
                chunks,
                document_id=manifest["document_id"],
                version_id=manifest["version_id"],
            )
            assets = list(nd.assets.values()) if file_format != "text" else []
            if file_format != "text":
                (stage / "normalized.json").write_text(
                    json.dumps(nd.to_dict(), ensure_ascii=False, indent=2)
                    + "\n",
                    encoding="utf-8",
                )
            verify_assets(stage, assets)
            if not assets:
                shutil.rmtree(temp_images, ignore_errors=True)
            if sha256_file(original) != source_sha256:
                raise ExtractionError("Source changed during extraction.")
            if sha256_file(artifact) != artifact_sha256:
                raise ExtractionError(
                    "Extraction artifact changed during extraction."
                )

            chunks_file = stage / "chunks.jsonl"
            with chunks_file.open("w", encoding="utf-8") as handle:
                for chunk in chunks:
                    handle.write(json.dumps(chunk, ensure_ascii=False) + "\n")
            _write_markdown(chunks, stage / "chunks.md")

            counts: dict[str, int] = {}
            origins: dict[str, int] = {}
            for chunk in chunks:
                counts[chunk["type"]] = counts.get(chunk["type"], 0) + 1
                origins[chunk["origin"]] = origins.get(chunk["origin"], 0) + 1

            chunk_manifest = {
                "schema_version": 1,
                "chunk_id": chunk_id,
                "document_id": manifest["document_id"],
                "document_version": manifest["version_id"],
                "source_sha256": source_sha256,
                "source_artifact": artifact_rel,
                "artifact_sha256": artifact_sha256,
                "source_format": file_format,
                "configuration": config,
                "status": "partial" if pages_spec else "ready",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "chunk_count": len(chunks),
                "counts": counts,
                "origins": origins,
                "warnings": warnings,
                "chunks_path": "chunks.jsonl",
                "preview_path": "chunks.md",
                "chunks_sha256": sha256_file(chunks_file),
                "assets": assets,
                "quality": (
                    coverage_report(nd, chunks, warnings)
                    if file_format != "text"
                    else {
                        "status": (
                            "passed_checks" if chunks else "needs_review"
                        ),
                        "scope": "Text chunking only",
                    }
                ),
            }
            if file_format != "text":
                chunk_manifest["normalized_path"] = "normalized.json"
                chunk_manifest["normalized_sha256"] = sha256_file(
                    stage / "normalized.json"
                )
            if review_markdown and file_format not in {"text"}:
                try:
                    review_prompt = vp.env.get_template(
                        "markdown_prompt.j2"
                    ).render(normalized_elements=nd.prompt_elements())
                    review = new_pipeline.run_text(review_prompt)
                    if review.strip():
                        (stage / "review.md").write_text(
                            review.strip() + "\n", encoding="utf-8"
                        )
                        chunk_manifest["review_path"] = "review.md"
                except Exception as exc:  # review is optional
                    warnings.append(f"review markdown failed: {exc}")
            # Include optional-review failures in the final quality summary.
            chunk_manifest["quality"] = (
                coverage_report(nd, chunks, warnings)
                if file_format != "text"
                else chunk_manifest["quality"]
            )
            (stage / "chunk_manifest.json").write_text(
                json.dumps(chunk_manifest, ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
            stage.rename(destination)
            return {
                "cached": False,
                "chunk_path": str(destination),
                "manifest": chunk_manifest,
            }
    finally:
        lock.unlink(missing_ok=True)


def extract_all(
    storage_root: str | Path,
    *,
    target_tokens: int = MAX_TOKENS_TARGET,
) -> dict:
    root = Path(storage_root).resolve()
    results = []
    errors = []
    for manifest_path in sorted(root.glob("*/sha256-*/manifest.json")):
        version = manifest_path.parent
        try:
            results.append(
                extract_version(version, target_tokens=target_tokens)
            )
        except (ExtractionError, RenderError, OSError, ValueError) as exc:
            errors.append({"version_path": str(version), "error": str(exc)})
    return {
        "status": "partial_failure" if errors else "ready",
        "results": results,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create retrieval chunks for a stored document version "
            "(checkpoint 3)."
        )
    )
    parser.add_argument(
        "version_path",
        nargs="?",
        type=Path,
        help="Version directory containing manifest.json",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Extract every stored version under --storage-root",
    )
    parser.add_argument(
        "--storage-root",
        type=Path,
        default=Path("data/processed/documents"),
    )
    parser.add_argument(
        "--target-tokens",
        type=int,
        default=MAX_TOKENS_TARGET,
        help="Soft chunk token target passed to the grouping prompt",
    )
    parser.add_argument(
        "--pages",
        help="Debug: extract only selected 1-based pages, e.g. '4,7-9'",
    )
    parser.add_argument(
        "--review-markdown",
        action="store_true",
        help=(
            "Also ask the model to render normalized elements "
            "as review Markdown"
        ),
    )
    args = parser.parse_args()

    try:
        if args.all:
            result = extract_all(
                args.storage_root, target_tokens=args.target_tokens
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "ready" else 1
        if args.version_path is None:
            parser.error("version_path is required unless --all is used")
        result = extract_version(
            args.version_path,
            target_tokens=args.target_tokens,
            pages_spec=args.pages,
            review_markdown=args.review_markdown,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ExtractionError, RenderError, OSError, ValueError) as exc:
        print(f"Extraction failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
