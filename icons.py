"""The shared-brain icon: served at the site root (favicon) and announced in the MCP handshake.

Clients show it in two ways, so both are covered:
  - MCP serverInfo.icons, sent on `initialize` (Claude, ChatGPT and others may read it);
  - /favicon.ico and friends on the server's own domain, which the viewer tab uses and which
    some clients fetch for a connector's logo.
The files live in static/ and are public on purpose: they hold nothing but the picture.
"""
import base64
import os
from pathlib import Path

from mcp.types import Icon
from starlette.requests import Request
from starlette.responses import Response

STATIC = Path(__file__).resolve().parent / "static"

FILES = {
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/favicon.svg": ("icon.svg", "image/svg+xml"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}


def public_url() -> str | None:
    """Base URL the server is reached at. Render sets RENDER_EXTERNAL_URL by itself."""
    url = os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    return url.rstrip("/") if url else None


def mcp_icons(base: str | None = None) -> list[Icon]:
    """Icons for serverInfo. Links to this server when its URL is known, else the SVG inline."""
    base = base if base is not None else public_url()
    if base:
        return [
            Icon(src=f"{base}/icon-192.png", mimeType="image/png", sizes=["192x192"]),
            Icon(src=f"{base}/icon-512.png", mimeType="image/png", sizes=["512x512"]),
            Icon(src=f"{base}/favicon.svg", mimeType="image/svg+xml", sizes=["any"]),
        ]
    svg = base64.b64encode((STATIC / "icon.svg").read_bytes()).decode()
    return [Icon(src=f"data:image/svg+xml;base64,{svg}", mimeType="image/svg+xml", sizes=["any"])]


def _endpoint(filename: str, media_type: str):
    body = (STATIC / filename).read_bytes()

    async def serve(_request: Request) -> Response:
        return Response(body, media_type=media_type,
                        headers={"Cache-Control": "public, max-age=86400"})
    return serve


def register(mcp) -> None:
    """Serve the icon files next to the MCP endpoint, through FastMCP's custom_route."""
    for path, (filename, media_type) in FILES.items():
        mcp.custom_route(path, methods=["GET"])(_endpoint(filename, media_type))
