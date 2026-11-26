"""TikTok Shop — API de afiliado CRIADOR (Partner Center → Affiliate Creator API).

Como funciona:
1. Você cria um app no TikTok Shop Partner Center (partner.tiktokshop.com) com as permissões
   de afiliado/criador e copia o App Key e o App Secret.
2. A sua conta de CRIADOR afiliado autoriza o app (botão "Conectar" no painel). O TikTok devolve
   um código, trocado por um token de acesso (renovado sozinho antes de vencer).
3. O bot busca produtos com comissão ("open collaboration") e gera o SEU link de afiliado.

Assinatura das chamadas (HMAC-SHA256): parâmetros da URL (menos sign e access_token) em ordem
alfabética, como chave+valor, com o caminho na frente e o corpo JSON no fim; tudo entre o
secret dos dois lados.

⚠️ A disponibilidade da API de afiliado no Brasil não está confirmada na documentação pública:
o teste de conexão no painel mostra a resposta real do TikTok para a sua conta.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import httpx

from offers.mercadolivre import MLProduct as Product

API_BASE = "https://open-api.tiktokglobalshop.com"
AUTH_BASE = "https://auth.tiktok-shops.com"
SEARCH_PATH = "/affiliate_creator/202405/open_collaborations/products/search"
LINKS_PATH = "/affiliate_creator/202505/affiliate_sharing_links/general_publishers/generate_batch"
PLATFORM = "tiktok"
CENT = Decimal("0.01")
REFRESH_BEFORE = 24 * 3600          # renova o token quando faltar menos de 1 dia
PRODUCT_RE = re.compile(r"/product/(\d{6,})|product_id=(\d{6,})|/(\d{15,})(?:[/?#]|$)")
TIKTOK_HOSTS = ("tiktok.com", "tiktokshop.com", "tokopedia.com")


class TikTokError(Exception):
    pass


class TikTokAuthError(TikTokError):
    """Token vencido/revogado ou app sem permissão: precisa conectar de novo."""


def sign(path: str, params: dict[str, Any], body: str, secret: str) -> str:
    items = sorted((k, v) for k, v in params.items() if k not in ("sign", "access_token"))
    base = path + "".join(f"{k}{v}" for k, v in items) + (body or "")
    return hmac.new(secret.encode(), f"{secret}{base}{secret}".encode(), hashlib.sha256).hexdigest()


def _money(value: Any) -> Decimal | None:
    try:
        v = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return v if v > 0 else None


def _amount(block: Any) -> Decimal | None:
    if not isinstance(block, dict):
        return None
    return _money(block.get("minimum_amount") or block.get("amount") or block.get("maximum_amount"))


def to_product(item: dict, source: str | None = None) -> Product | None:
    price = _amount(item.get("sales_price")) or _amount(item.get("original_price"))
    pid = str(item.get("id") or "")
    if price is None or not pid:
        return None
    original = _amount(item.get("original_price"))
    commission = item.get("commission") or {}
    try:
        rate = Decimal(str(commission.get("rate"))) / Decimal(10000)   # 1000 = 10%
        rate = rate.quantize(Decimal("0.0001")) if 0 < rate < 1 else None
    except (InvalidOperation, TypeError, ValueError):
        rate = None
    try:
        sold = int(item.get("units_sold")) if item.get("units_sold") is not None else None
    except (TypeError, ValueError):
        sold = None
    cats = item.get("category_chains") or []
    return Product(
        external_id=pid,
        title=str(item.get("title") or "")[:300] or "Produto TikTok Shop",
        url=item.get("detail_link") or f"https://shop.tiktok.com/view/product/{pid}",
        image=item.get("main_image_url") or None,
        price=price,
        original_price=original if original and original > price else None,
        category_id=str(cats[-1].get("id")) if cats and isinstance(cats[-1], dict) and cats[-1].get("id") else None,
        free_shipping=False,
        source=source,
        raw=item,
        platform=PLATFORM,
        commission_rate=rate,
        sold=sold,
    )


def parse_product_id(url: str) -> str | None:
    m = PRODUCT_RE.search(url or "")
    return next((g for g in m.groups() if g), None) if m else None


# --------------------------------------------------------------------------- #
# Autorização (token da conta de criador)
# --------------------------------------------------------------------------- #
def _auth_call(path: str, params: dict, transport: httpx.BaseTransport | None) -> dict:
    try:
        with httpx.Client(timeout=20, transport=transport) as http:
            r = http.get(AUTH_BASE + path, params=params)
        data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise TikTokError(f"Não consegui falar com o TikTok agora ({exc.__class__.__name__}).") from exc
    if data.get("code") not in (0, "0") or not isinstance(data.get("data"), dict):
        raise TikTokAuthError(f"O TikTok recusou a autorização: {data.get('message') or 'erro desconhecido'}")
    return data["data"]


def exchange_code(app_key: str, app_secret: str, code: str, *, transport: httpx.BaseTransport | None = None) -> dict:
    """Troca o código da autorização por tokens. Devolve access_token, refresh_token, expires, name."""
    d = _auth_call("/api/v2/token/get", {"app_key": app_key, "app_secret": app_secret, "auth_code": code.strip(),
                                          "grant_type": "authorized_code"}, transport)
    return _tokens(d)


def refresh(app_key: str, app_secret: str, refresh_token: str, *,
            transport: httpx.BaseTransport | None = None) -> dict:
    d = _auth_call("/api/v2/token/refresh", {"app_key": app_key, "app_secret": app_secret,
                                              "refresh_token": refresh_token, "grant_type": "refresh_token"}, transport)
    return _tokens(d)


def _tokens(d: dict) -> dict:
    if not d.get("access_token"):
        raise TikTokAuthError("O TikTok não devolveu o token de acesso.")
    return {"access_token": d["access_token"], "refresh_token": d.get("refresh_token") or "",
            "expires": int(d.get("access_token_expire_in") or 0),
            "name": str(d.get("user_name") or d.get("seller_name") or d.get("open_id") or "")[:80]}


def auth_link(auth_url: str, state: str) -> str:
    """Link de autorização do app (copiado do Partner Center) com o `state` desta sessão."""
    sep = "&" if "?" in auth_url else "?"
    return f"{auth_url}{sep}state={state}"


# --------------------------------------------------------------------------- #
# Cliente
# --------------------------------------------------------------------------- #
class TikTokClient:
    def __init__(self, app_key: str, app_secret: str, access_token: str, *, timeout: float = 20.0,
                 transport: httpx.BaseTransport | None = None, clock=time.time):
        if not (app_key and app_secret):
            raise TikTokAuthError("Cadastre o App Key e o App Secret do TikTok Shop em Lojas e APIs.")
        if not access_token:
            raise TikTokAuthError("Conecte a sua conta de criador do TikTok Shop em Lojas e APIs.")
        self.app_key, self._secret, self._token, self._clock = app_key.strip(), app_secret.strip(), access_token, clock
        self._http = httpx.Client(timeout=timeout, transport=transport, base_url=API_BASE)

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _post(self, path: str, query: dict[str, Any], body: dict) -> dict:
        params = {"app_key": self.app_key, "timestamp": int(self._clock()), **query}
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        params["sign"] = sign(path, params, raw, self._secret)
        try:
            r = self._http.post(path, params=params, content=raw.encode(),
                                headers={"content-type": "application/json", "x-tts-access-token": self._token})
        except httpx.HTTPError as exc:
            raise TikTokError(f"TikTok Shop fora do ar ({exc.__class__.__name__}).") from exc
        try:
            data = r.json()
        except ValueError as exc:
            raise TikTokError(f"Resposta inválida do TikTok Shop (HTTP {r.status_code}).") from exc
        code = data.get("code")
        if r.status_code in (401, 403) or (code not in (0, "0") and "token" in str(data.get("message", "")).lower()):
            raise TikTokAuthError("A conexão com o TikTok Shop venceu ou foi revogada: conecte de novo em Lojas e APIs.")
        if code not in (0, "0"):
            raise TikTokError(f"TikTok Shop: {data.get('message') or 'erro'} (código {code}).")
        return data.get("data") or {}

    def search(self, keywords: list[str] | None = None, *, sort: str = "commission_rate", min_commission: int = 0,
               page_size: int = 20, page_token: str | None = None, source: str | None = None) -> list[Product]:
        body: dict[str, Any] = {}
        if keywords:
            body["title_keywords"] = [k[:50] for k in keywords][:20]
        if min_commission:
            body["commission_rate_range"] = {"rate_ge": int(min_commission) * 100}
        query: dict[str, Any] = {"page_size": max(1, min(page_size, 20)), "sort_field": sort, "sort_order": "DESC"}
        if sort == "product_sales_price":
            query["sort_order"] = "ASC"
        if page_token:
            query["page_token"] = page_token
        data = self._post(SEARCH_PATH, query, body)
        out = []
        for item in data.get("products") or []:
            p = to_product(item, source)
            if p is not None and item.get("has_inventory", True) is not False:
                out.append(p)
        return out

    def affiliate_links(self, product_ids: list[str]) -> dict[str, str]:
        """{id do produto: link de afiliado} — em lotes de até 50."""
        links: dict[str, str] = {}
        for i in range(0, len(product_ids), 50):
            batch = product_ids[i:i + 50]
            data = self._post(LINKS_PATH, {}, {"material": {"type": "PRODUCT", "ids": batch}})
            for row in data.get("sharing_links") or []:
                link = row.get("sharing_link") or row.get("one_link") or row.get("deep_link")
                if row.get("material_id") and link:
                    links[str(row["material_id"])] = link
        return links

    def search_with_links(self, keywords: list[str] | None = None, **kwargs) -> list[Product]:
        """Busca e já coloca o SEU link de afiliado; produto sem link fica de fora (não pagaria comissão)."""
        products = self.search(keywords, **kwargs)
        if not products:
            return []
        links = self.affiliate_links([p.external_id for p in products])
        out = []
        for p in products:
            if p.external_id in links:
                p.affiliate_url = links[p.external_id]
                out.append(p)
        return out

    def test_connection(self) -> list[tuple[str, bool, str]]:
        try:
            found = self.search(page_size=1, source="teste")
            return [("Busca de produtos com comissão", True, f"{len(found)} produto(s) na amostra")]
        except TikTokError as exc:
            return [("Busca de produtos com comissão", False, str(exc))]
