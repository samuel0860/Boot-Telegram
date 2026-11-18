"""Cliente da API OFICIAL do Mercado Livre (https://api.mercadolibre.com).

Autenticação: OAuth 2.0 "client_credentials" com o App ID (client_id) e a
Secret Key (client_secret) da sua aplicação em
https://developers.mercadolivre.com.br/devcenter — o token vale ~6 horas e é
renovado automaticamente.

Fontes de produtos usadas:
- Mais vendidos por categoria:  GET /highlights/MLB/category/{id}
- Busca no catálogo:            GET /products/search?site_id=MLB&q=...
- Detalhes do produto:          GET /products/{id}   (buy_box_winner = melhor oferta)
- Preço promocional:            GET /items/{id}/sale_price  (amount x regular_amount)
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API = "https://api.mercadolibre.com"
SITE = "MLB"
CENT = Decimal("0.01")


class MLError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class MLAuthError(MLError):
    pass


@dataclass
class MLProduct:
    external_id: str
    title: str
    url: str
    image: str | None
    price: Decimal
    original_price: Decimal | None
    category_id: str | None
    free_shipping: bool = False
    source: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    platform: str = "mercadolivre"
    # link de afiliado que a própria API já devolve (Shopee); no Mercado Livre é colado à mão
    affiliate_url: str | None = None

    @property
    def discount_pct(self) -> int | None:
        if self.original_price and self.original_price > self.price > 0:
            return int(((1 - self.price / self.original_price) * 100).to_integral_value(rounding="ROUND_HALF_UP"))
        return None


def _money(value: Any) -> Decimal | None:
    if value in (None, "", 0):
        return None
    try:
        d = Decimal(str(value)).quantize(CENT)
    except (InvalidOperation, ValueError):
        return None
    return d if d > 0 else None


def _https(url: str | None) -> str | None:
    if not url:
        return None
    return "https://" + url[len("http://"):] if url.startswith("http://") else url


# Cache de token por App ID (compartilhado entre threads do mesmo processo)
_token_cache: dict[str, tuple[str, float]] = {}
_token_lock = threading.Lock()


class MercadoLivreClient:
    def __init__(self, app_id: str, secret_key: str, *, timeout: float = 20.0,
                 transport: httpx.BaseTransport | None = None):
        if not app_id or not secret_key:
            raise MLAuthError("Informe o App ID e a Secret Key do Mercado Livre no painel.")
        self.app_id = app_id.strip()
        self._secret = secret_key.strip()
        self._http = httpx.Client(base_url=API, timeout=timeout, transport=transport,
                                  headers={"Accept": "application/json", "User-Agent": "telegram-vendas/1.0"})

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ auth
    def _fetch_token(self) -> str:
        try:
            r = self._http.post("/oauth/token", data={
                "grant_type": "client_credentials", "client_id": self.app_id, "client_secret": self._secret,
            }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        except httpx.HTTPError as exc:
            raise MLError(f"Falha de conexão com o Mercado Livre: {exc.__class__.__name__}") from exc
        data = _json(r)
        if r.status_code != 200 or not data.get("access_token"):
            msg = data.get("message") or data.get("error_description") or data.get("error") or r.text[:200]
            raise MLAuthError(f"Credenciais recusadas pelo Mercado Livre (HTTP {r.status_code}): {msg}", r.status_code)
        ttl = float(data.get("expires_in") or 21600)
        with _token_lock:
            _token_cache[self.app_id] = (data["access_token"], time.time() + ttl - 300)
        return data["access_token"]

    def _token(self, force: bool = False) -> str:
        with _token_lock:
            cached = _token_cache.get(self.app_id)
        if cached and not force and cached[1] > time.time():
            return cached[0]
        return self._fetch_token()

    def _get(self, path: str, params: dict | None = None) -> Any:
        for attempt in range(2):
            token = self._token(force=attempt > 0)
            try:
                r = self._http.get(path, params=params, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as exc:
                raise MLError(f"Falha de conexão com o Mercado Livre: {exc.__class__.__name__}") from exc
            if r.status_code == 401 and attempt == 0:
                continue  # token expirado: renova e tenta de novo
            if r.status_code == 429:
                raise MLError("Limite de requisições do Mercado Livre atingido (HTTP 429). Aumente o intervalo.", 429)
            if r.status_code >= 400:
                data = _json(r)
                msg = data.get("message") or data.get("error") or r.text[:200]
                raise MLError(f"GET {path} -> HTTP {r.status_code}: {msg}", r.status_code)
            return _json(r)
        raise MLAuthError("Token recusado pelo Mercado Livre.", 401)

    # --------------------------------------------------------------- fontes
    def highlights(self, category_id: str) -> list[tuple[str, str]]:
        """Mais vendidos da categoria: lista de (id, tipo) — tipo PRODUCT, ITEM ou USER_PRODUCT."""
        data = self._get(f"/highlights/{SITE}/category/{category_id}")
        return [(str(c["id"]), str(c.get("type", "")).upper()) for c in data.get("content", []) if c.get("id")]

    def search_products(self, query: str, limit: int = 20) -> list[str]:
        data = self._get("/products/search", {"status": "active", "site_id": SITE, "q": query, "limit": limit})
        return [str(r["id"]) for r in data.get("results", []) if r.get("id")]

    def category_name(self, category_id: str) -> str | None:
        try:
            return self._get(f"/categories/{category_id}").get("name")
        except MLError:
            return None

    def _sale_price(self, item_id: str) -> tuple[Decimal | None, Decimal | None]:
        try:
            data = self._get(f"/items/{item_id}/sale_price", {"context": "channel_marketplace"})
        except MLError:
            return None, None
        return _money(data.get("amount")), _money(data.get("regular_amount"))

    def product(self, product_id: str, source: str | None = None) -> MLProduct | None:
        """Produto de catálogo com o preço da melhor oferta (buy box)."""
        try:
            data = self._get(f"/products/{product_id}")
        except MLError as exc:
            if exc.status in (403, 404):
                return None
            raise
        bbw = data.get("buy_box_winner") or {}
        price = _money(bbw.get("price"))
        original = _money(bbw.get("original_price"))
        item_id = bbw.get("item_id")
        if item_id and original is None:
            # A API de preços é a fonte oficial do preço promocional vigente.
            sale, regular = self._sale_price(str(item_id))
            if sale:
                price = sale
                original = regular if regular and regular > sale else None
        if price is None:
            return None  # sem oferta ativa
        pictures = data.get("pictures") or []
        image = _https(pictures[0].get("url")) if pictures else None
        shipping = bbw.get("shipping") or {}
        return MLProduct(
            external_id=str(data.get("id") or product_id),
            title=(data.get("name") or "").strip()[:300] or product_id,
            url=data.get("permalink") or f"https://www.mercadolivre.com.br/p/{product_id}",
            image=image,
            price=price,
            original_price=original,
            category_id=bbw.get("category_id"),
            free_shipping=bool(shipping.get("free_shipping")),
            source=source,
            raw={"id": data.get("id"), "item_id": item_id},
        )

    def item(self, item_id: str, source: str | None = None) -> MLProduct | None:
        """Anúncio (ITEM). Alguns anúncios de terceiros podem retornar 403 — nesse caso é ignorado."""
        try:
            data = self._get(f"/items/{item_id}")
        except MLError as exc:
            if exc.status in (403, 404):
                return None
            raise
        if data.get("status") not in (None, "active"):
            return None
        sale, regular = self._sale_price(item_id)
        price = sale or _money(data.get("price"))
        original = regular if (regular and price and regular > price) else _money(data.get("original_price"))
        if price is None:
            return None
        pictures = data.get("pictures") or []
        image = _https(pictures[0].get("secure_url") or pictures[0].get("url")) if pictures else _https(data.get("thumbnail"))
        return MLProduct(
            external_id=str(data.get("id") or item_id),
            title=(data.get("title") or item_id).strip()[:300],
            url=data.get("permalink") or f"https://produto.mercadolivre.com.br/{item_id}",
            image=image,
            price=price,
            original_price=original,
            category_id=data.get("category_id"),
            free_shipping=bool((data.get("shipping") or {}).get("free_shipping")),
            source=source,
            raw={"id": data.get("id")},
        )

    # ---------------------------------------------------------- diagnóstico
    def test_connection(self, category_id: str = "MLB1051") -> list[tuple[str, bool, str]]:
        """Executa cada etapa e retorna [(etapa, ok, detalhe)] para exibir no painel."""
        steps: list[tuple[str, bool, str]] = []
        try:
            self._token(force=True)
            steps.append(("Autenticação (App ID + Secret Key)", True, "token obtido"))
        except MLError as exc:
            steps.append(("Autenticação (App ID + Secret Key)", False, str(exc)))
            return steps
        try:
            hl = self.highlights(category_id)
            steps.append((f"Mais vendidos ({category_id})", True, f"{len(hl)} itens"))
        except MLError as exc:
            hl = []
            steps.append((f"Mais vendidos ({category_id})", False, str(exc)))
        try:
            ids = self.search_products("fone bluetooth", limit=5)
            steps.append(("Busca no catálogo", True, f"{len(ids)} produtos"))
        except MLError as exc:
            ids = []
            steps.append(("Busca no catálogo", False, str(exc)))
        sample = next((i for i, t in hl if t == "PRODUCT"), None) or (ids[0] if ids else None)
        if sample:
            try:
                p = self.product(sample)
                detail = (f"{p.title[:50]} — R$ {p.price} (de {p.original_price or '—'})" if p
                          else "produto sem oferta ativa")
                steps.append(("Detalhes e preço do produto", p is not None, detail))
            except MLError as exc:
                steps.append(("Detalhes e preço do produto", False, str(exc)))
        return steps


def _json(r: httpx.Response) -> dict:
    try:
        data = r.json()
        return data if isinstance(data, dict) else {"results": data}
    except ValueError:
        return {}


# Nome neutro: o mesmo formato serve para produtos do Mercado Livre e da Shopee.
Product = MLProduct
