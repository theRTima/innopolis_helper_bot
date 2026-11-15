"""Build deep links to a document viewer without copying files per user.

The viewer base URL must serve an already stored document and enforce access
control. Locators are stable document coordinates, not local filesystem paths.
"""

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import hmac
import time
from urllib.parse import quote, urlencode, urlsplit, urlunsplit


class LocatorKind(StrEnum):
    PDF_PAGE = "pdf-page"
    DOCX_PARAGRAPH = "docx-paragraph"
    XLSX_RANGE = "xlsx-range"
    PPTX_SLIDE = "pptx-slide"


@dataclass(frozen=True)
class Locator:
    """A stable position within a source document."""

    kind: LocatorKind
    value: str | int
    subvalue: str | None = None

    def __post_init__(self) -> None:
        if self.kind in {LocatorKind.PDF_PAGE, LocatorKind.PPTX_SLIDE}:
            if not isinstance(self.value, int) or self.value < 1:
                raise ValueError(f"{self.kind} value must be a positive integer")
        elif not str(self.value).strip():
            raise ValueError(f"{self.kind} value must not be empty")
        if self.kind == LocatorKind.XLSX_RANGE and not self.subvalue:
            raise ValueError("XLSX_RANGE requires subvalue with a cell range")

    def fragment(self) -> str:
        if self.kind == LocatorKind.PDF_PAGE:
            return f"page={self.value}"
        if self.kind == LocatorKind.PPTX_SLIDE:
            return f"slide={self.value}"
        if self.kind == LocatorKind.DOCX_PARAGRAPH:
            return urlencode({"paragraph": str(self.value)})
        return urlencode({"sheet": str(self.value), "range": self.subvalue or ""})


def sign_document_id(document_id: str, secret: str, expires_at: int) -> str:
    message = f"{document_id}:{expires_at}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def build_anchor_url(
    viewer_base_url: str,
    document_id: str,
    locator: Locator,
    *,
    secret: str,
    ttl_seconds: int = 7 * 24 * 60 * 60,
    now: int | None = None,
    view: str | None = None,
) -> str:
    """Return a URL of the form ``<viewer>/documents/<id>#<locator>``.

    ``viewer_base_url`` should point at the authenticated document viewer,
    e.g. ``https://docs.example.org``. The viewer implements fragment handling.
    """

    parsed = urlsplit(viewer_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("viewer_base_url must be an absolute HTTP(S) URL")
    if not document_id.strip():
        raise ValueError("document_id must not be empty")
    if not secret:
        raise ValueError("secret must not be empty")
    if ttl_seconds < 1:
        raise ValueError("ttl_seconds must be positive")

    path = f"{parsed.path.rstrip('/')}/documents/{quote(document_id, safe='')}"
    expires_at = (int(time.time()) if now is None else now) + ttl_seconds
    if view not in {None, "rendered"}:
        raise ValueError("Unknown document view")
    params = {"expires": expires_at, "signature": sign_document_id(document_id, secret, expires_at)}
    if view:
        params["view"] = view
    query = urlencode(params)
    return urlunsplit((parsed.scheme, parsed.netloc, path, query, locator.fragment()))
