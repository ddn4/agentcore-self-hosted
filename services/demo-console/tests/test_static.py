"""The static page contract (DESIGN.md 4.1, 6.1): local assets only, every module exists.

Fonts are vendored under ``static/vendor/fonts`` (docs/console/BRAND.md): no CDN at runtime.

The page itself is built by the frontend half; these checks skip until ``index.html`` exists.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from demo_console.app import STATIC_DIR, create_app
from demo_console.fakes import FakeGateway, FakeHttp, FakeRunner, make_settings

pytestmark = pytest.mark.unit
INDEX = STATIC_DIR / "index.html"
needs_page = pytest.mark.skipif(not INDEX.is_file(), reason="static/index.html not built yet")


@needs_page
def test_index_references_only_local_assets() -> None:
    html = INDEX.read_text()
    for ref in re.findall(r'(?:src|href)="([^"]+)"', html):
        if ref.startswith(("#", "data:")):
            continue
        assert not ref.startswith(("http:", "https:", "//")), ref  # fonts are vendored (BRAND.md)
        assert not ref.startswith("/") or ref.startswith("/api/"), ref
        assert (STATIC_DIR / ref.split("?")[0]).is_file(), ref


@needs_page
def test_fonts_are_vendored_with_their_licences() -> None:
    css = (STATIC_DIR / "styles.css").read_text()
    urls = re.findall(r"url\(\"?([^\")]+\.woff2)\"?\)", css)
    assert urls, "styles.css declares no @font-face"
    for url in urls:
        assert not url.startswith(("http:", "https:", "//")), url
        assert (STATIC_DIR / url).is_file(), url
    fonts = STATIC_DIR / "vendor" / "fonts"
    assert sorted(p.name for p in fonts.glob("OFL-*.txt")) == [
        "OFL-HankenGrotesk.txt",
        "OFL-IBMPlexMono.txt",
    ]
    assert "@import" not in css


@needs_page
def test_every_imported_module_exists() -> None:
    for js in STATIC_DIR.rglob("*.js"):
        for spec in re.findall(
            r"""(?:import|from)\s*\(?\s*['"](\.{1,2}/[^'"]+)['"]""", js.read_text()
        ):
            assert (js.parent / spec).resolve().is_file(), f"{js.name}: {spec}"


@needs_page
def test_no_localhost() -> None:
    for path in STATIC_DIR.rglob("*"):
        if path.is_file() and path.suffix in (".html", ".js", ".css", ".json"):
            assert "localhost" not in path.read_text(), path


async def test_placeholder_without_a_page(tmp_path: Path) -> None:
    app = create_app(
        make_settings(tmp_path),
        clients=FakeGateway(),
        runner=FakeRunner(),
        http=FakeHttp(),
        static_dir=tmp_path / "empty",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8099") as client:
        resp = await client.get("/")
    assert resp.status_code == 200 and "/api/stack" in resp.text


@needs_page
async def test_page_is_served_at_root(tmp_path: Path) -> None:
    app = create_app(
        make_settings(tmp_path), clients=FakeGateway(), runner=FakeRunner(), http=FakeHttp()
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8099") as client:
        assert (await client.get("/")).text == INDEX.read_text()
        assert (await client.get("/api/config")).status_code == 200
