"""Browser security headers for the pages the API serves: the test console and any static pages.

Every response gets `nosniff`, a referrer policy and HSTS. HTML pages also get a Content-Security-Policy: no inline or
third-party script on the console (it holds an API key in the browser), and `frame-ancestors 'none'` everywhere, so no
other site can frame them. Public chat routes set their own framing rules
(an embed may be framed by its allowed sites) and are left alone, as is the API documentation."""

from __future__ import annotations

from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

BASE = {
    b"x-content-type-options": b"nosniff",
    b"referrer-policy": b"strict-origin-when-cross-origin",
    b"strict-transport-security": b"max-age=31536000",
}
#: shared by both pages: nothing framed, no plugins, forms and <base> only to this origin
COMMON = "default-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
CONSOLE = (
    f"{COMMON}; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; "
    "connect-src 'self'; worker-src 'self' blob:; frame-src 'self'"
)


def site_policy(connect: list[str]) -> str:
    return (
        f"{COMMON}; script-src 'self' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        f"font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src {' '.join(["'self'", *connect])}"
    )


class SecurityHeaders:
    def __init__(self, app: ASGIApp, skip: tuple[str, ...]):
        self.app, self.skip = app, skip
        self.site = site_policy([]).encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers: list[Any] = message.setdefault("headers", [])
                have = {k.lower() for k, _ in headers}
                headers.extend((k, v) for k, v in BASE.items() if k not in have)
                html = any(k.lower() == b"content-type" and v.startswith(b"text/html") for k, v in headers)
                if html and not path.startswith(self.skip) and b"content-security-policy" not in have:
                    headers.append((b"content-security-policy", CONSOLE.encode() if path.startswith("/console") else self.site))
                    headers.append((b"x-frame-options", b"DENY"))
            await send(message)

        await self.app(scope, receive, with_headers)
