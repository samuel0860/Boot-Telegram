"""App instalável (PWA): manifesto, service worker, ícones e tags no painel."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import app


def test_manifest_and_service_worker_are_public():
    with TestClient(app) as c:
        m = c.get("/admin/manifest.webmanifest")
        assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
        data = m.json()
        assert data["start_url"] == "/admin/" and data["scope"] == "/admin/" and data["display"] == "standalone"
        assert {i["purpose"] for i in data["icons"]} == {"any", "maskable"}
        for icon in data["icons"]:
            r = c.get(icon["src"])
            assert r.status_code == 200 and r.headers["content-type"] == "image/png"

        sw = c.get("/admin/sw.js")
        assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
        assert 'const CACHE = "painel-' in sw.text and "/static/pwa/offline.html" in sw.text
        assert sw.headers["cache-control"] == "no-store"          # o app pega a versão nova logo
        assert c.get("/static/pwa/offline.html").status_code == 200


def test_login_page_links_manifest():
    with TestClient(app) as c:
        page = c.get("/admin/login").text
    assert 'rel="manifest" href="/admin/manifest.webmanifest"' in page
    assert 'rel="apple-touch-icon"' in page and 'name="theme-color"' in page
