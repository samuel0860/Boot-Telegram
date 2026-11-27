"""App instalável (PWA): manifesto, service worker e página "sem internet".

Ficam em /admin/ para o app abrir direto no painel (escopo /admin/). Não exigem login:
o navegador busca esses arquivos antes de qualquer sessão.
"""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

from config import BASE_DIR, settings

router = APIRouter()

STATIC = BASE_DIR / "static"
# arquivos guardados no aparelho (abrem mesmo sem internet)
ASSETS = ["/static/css/admin.css", "/static/js/admin.js", "/static/pwa/offline.html",
          "/static/pwa/icon-192.png", "/static/images/favicon.svg"]
THEME = "#0b0e14"

MANIFEST = {
    "name": settings.app_name,
    "short_name": settings.app_name[:12],
    "description": "Painel do bot de vendas e dos bots de promoção.",
    "id": "/admin/",
    "start_url": "/admin/",
    "scope": "/admin/",
    "display": "standalone",
    "orientation": "any",
    "lang": "pt-BR",
    "background_color": THEME,
    "theme_color": THEME,
    "icons": [
        {"src": "/static/pwa/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/static/pwa/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/static/pwa/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
    "shortcuts": [
        {"name": "Central dos bots", "url": "/admin/offers/bots",
         "icons": [{"src": "/static/pwa/icon-192.png", "sizes": "192x192"}]},
        {"name": "Ofertas encontradas", "url": "/admin/offers",
         "icons": [{"src": "/static/pwa/icon-192.png", "sizes": "192x192"}]},
        {"name": "Pedidos", "url": "/admin/orders",
         "icons": [{"src": "/static/pwa/icon-192.png", "sizes": "192x192"}]},
    ],
}


def _version() -> str:
    """Muda sempre que o CSS/JS/ícones mudam: o app instalado se atualiza sozinho."""
    h = hashlib.sha256()
    for path in ASSETS:
        f = STATIC / path.removeprefix("/static/")
        if f.exists():
            h.update(f.read_bytes())
    return h.hexdigest()[:12]


SW_TEMPLATE = """// Service worker do painel (gerado em admin/views/pwa.py)
const CACHE = "painel-__VERSION__";
const ASSETS = __ASSETS__;
const OFFLINE = "/static/pwa/offline.html";

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("painel-") && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Páginas do painel: sempre da internet (dados ao vivo, nada fica guardado no aparelho).
  // Sem conexão, mostra a página "sem internet".
  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match(OFFLINE)));
    return;
  }
  // CSS, JS e ícones: abre rápido com o que está guardado e atualiza em segundo plano.
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.open(CACHE).then((cache) =>
        cache.match(req).then((cached) => {
          const network = fetch(req).then((res) => {
            if (res.ok) cache.put(req, res.clone());
            return res;
          }).catch(() => cached);
          return cached || network;
        })
      )
    );
  }
});
"""


@router.get("/manifest.webmanifest")
def manifest():
    return JSONResponse(MANIFEST, media_type="application/manifest+json")


@router.get("/sw.js")
def service_worker():
    body = SW_TEMPLATE.replace("__VERSION__", _version()).replace("__ASSETS__", json.dumps(ASSETS))
    return Response(body, media_type="text/javascript", headers={"Service-Worker-Allowed": "/admin/"})
