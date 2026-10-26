"""Tests for the icon: files served at the site root and the icons announced in serverInfo."""
import pytest
from starlette.testclient import TestClient

import icons
import server


@pytest.fixture(scope="module")
def client():
    return TestClient(server.mcp.streamable_http_app())


@pytest.mark.parametrize("path", list(icons.FILES))
def test_icon_files_are_served(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(icons.FILES[path][1])
    assert len(r.content) > 100


def test_png_and_ico_are_real_images(client):
    assert client.get("/icon-192.png").content.startswith(b"\x89PNG")
    assert client.get("/favicon.ico").content[:4] == b"\x00\x00\x01\x00"


def test_icons_link_to_the_server_when_its_url_is_known():
    got = icons.mcp_icons("https://example.onrender.com")
    assert got[0].src == "https://example.onrender.com/icon-192.png"
    assert all(i.src.startswith("https://example.onrender.com/") for i in got)


def test_icons_fall_back_to_inline_svg(monkeypatch):
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    (only,) = icons.mcp_icons()
    assert only.src.startswith("data:image/svg+xml;base64,")


def test_server_announces_icons():
    assert server.mcp._mcp_server.icons
    assert server.mcp._mcp_server.website_url == "https://github.com/anqiXu33/shared-brain"
