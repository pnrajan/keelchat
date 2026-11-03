"""Content people give to build a chatbot: uploaded files and web pages, turned into text documents a dataset can point
at.

The text is kept through the artifact store (`yachat/state/artifacts.py`) under `uploads/`: S3 when
YACHAT_UPLOADS_BUCKET is set (list it in YACHAT_DATA_BUCKETS too), else a folder under
YACHAT_DATA_ROOT. Either way it returns URIs the Knowledge Service may read, so the documents become an ordinary
unstructured dataset.

Web pages are fetched only from public addresses (no loopback, private or metadata hosts), each redirect re-checked,
with a size and time limit."""

from __future__ import annotations

import asyncio
import io
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from yachat.logging import get_logger

log = get_logger("uploads")

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_FILES = 20
MAX_URLS = 10
TEXT_TYPES = (".txt", ".md", ".markdown", ".csv")


class UploadRejected(Exception):
    """Content that can't be used; the message is written for the person who gave it."""


# ---------------------------------------------------------------------------- turning content into text


class _Text(HTMLParser):
    """Visible text of an HTML page: headings and paragraphs kept on their own lines, scripts and styles dropped."""

    SKIP = {"script", "style", "noscript", "svg", "template", "head"}
    BLOCK = {"p", "div", "section", "article", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "header", "footer", "main", "td", "th"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)


def html_text(html: str) -> tuple[str, str]:
    """(title, text) of an HTML page."""
    p = _Text()
    p.feed(html)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.parts))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return p.title.strip(), text


def file_text(name: str, data: bytes) -> str:
    """Text of an uploaded file: plain text, Markdown, CSV, HTML or PDF."""
    if len(data) > MAX_FILE_BYTES:
        raise UploadRejected(f"{name} is larger than 10 MB")
    lower = name.lower()
    if lower.endswith(".pdf"):
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError

        try:
            pages = [page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages]
        except (PdfReadError, ValueError) as e:
            raise UploadRejected(f"{name} couldn't be read as a PDF") from e
        text = "\n\n".join(p.strip() for p in pages if p.strip())
    elif lower.endswith((".html", ".htm")):
        text = html_text(data.decode("utf-8", errors="replace"))[1]
    elif lower.endswith(TEXT_TYPES):
        text = data.decode("utf-8", errors="replace")
    else:
        raise UploadRejected(f"{name}: use PDF, text, Markdown, CSV or HTML files")
    if not text.strip():
        raise UploadRejected(f"{name} has no text to read (a scanned PDF needs text recognition first)")
    return text


async def fetch_raw(url: str, agent: str = "yachatBot/1.0 (+content import)") -> tuple[str, bytes, str, str]:
    """(final address, body, content type, encoding) of a public web page. Every address it reaches, redirects included,
    must be public."""
    import httpx

    from yachat.registry.tenant_tools import TenantToolRejected, check_public_url

    if urlparse(url).scheme not in ("http", "https"):
        raise UploadRejected(f"{url}: use an http(s) address")
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False, headers={"User-Agent": agent}) as c:
        for _ in range(4):
            try:
                await asyncio.to_thread(check_public_url, url)
            except TenantToolRejected as e:
                raise UploadRejected(f"{url} isn't a public address") from e
            try:
                async with c.stream("GET", url) as r:
                    if r.is_redirect and "location" in r.headers:
                        url = urljoin(url, r.headers["location"])
                        continue
                    if r.status_code >= 400:
                        raise UploadRejected(f"{url} answered {r.status_code}")
                    body = b""
                    async for chunk in r.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_PAGE_BYTES:
                            raise UploadRejected(f"{url} is larger than 3 MB")
                    return url, body, r.headers.get("content-type", ""), r.encoding or "utf-8"
            except httpx.HTTPError as e:
                raise UploadRejected(f"{url} couldn't be fetched") from e
    raise UploadRejected(f"{url} redirects too many times")


async def fetch_page(url: str) -> tuple[str, str]:
    """(title, text) of a public web page. Every address it reaches, redirects included, must be public."""
    url, body, kind, encoding = await fetch_raw(url)
    if "html" in kind:
        title, text = html_text(body.decode(encoding, errors="replace"))
    elif kind.startswith("text/"):
        title, text = "", body.decode(encoding, errors="replace")
    elif "pdf" in kind:
        title, text = "", file_text("page.pdf", body)
    else:
        raise UploadRejected(f"{url} isn't a web page or text ({kind or 'unknown type'})")
    if not text.strip():
        raise UploadRejected(f"{url} has no readable text (pages that need JavaScript to show content can't be read)")
    return title or urlparse(url).netloc, text


def slug(text: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n] or "doc"
