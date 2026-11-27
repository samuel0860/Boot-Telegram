"""Cliente da API OFICIAL de afiliados da Shopee (Shopee Affiliate Open API, GraphQL).

Credenciais: AppID e Senha (Secret) em affiliate.shopee.com.br → Open API
(o programa de afiliados precisa estar aprovado).

Autenticação de cada requisição (cabeçalho Authorization):
    SHA256 Credential={AppID}, Timestamp={unix}, Signature={sha256(AppID + Timestamp + corpo + Secret)}

Fontes usadas:
- productOfferV2(keyword | productCatId | itemId+shopId, sortType, page, limit)
  (os valores vão escritos na própria consulta, sem variáveis tipadas)
  -> nome, preço, % de desconto, imagem, link do produto e **offerLink** (já é o
     link de afiliado da sua conta, com a sua comissão);
- generateShortLink(originUrl) -> link curto de afiliado (usado se faltar o offerLink).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import httpx

from offers.mercadolivre import Product

API = "https://open-api.affiliate.shopee.com.br/graphql"
PLATFORM = "shopee"
CENT = Decimal("0.01")

# sortType da productOfferV2
SORTS = {"relevance": 1, "sales": 2, "price_desc": 3, "price_asc": 4, "commission": 5}
SORT_LABELS = {"sales": "Mais vendidos", "commission": "Maior comissão", "relevance": "Relevância",
               "price_asc": "Menor preço"}

OFFER_FIELDS = ("itemId shopId productName priceMin priceMax priceDiscountRate imageUrl productLink offerLink "
                "commissionRate sales ratingStar shopName productCatIds")

# https://shopee.com.br/Nome-do-produto-i.123456.7891011  |  https://shopee.com.br/product/123456/7891011
# -i.LOJA.ITEM (página), /product/LOJA/ITEM e /opaanlp/LOJA/ITEM (destino dos links curtos s.shopee.com.br)
ITEM_RE = re.compile(r"-i\.(\d+)\.(\d+)|/(?:product|opaanlp|[a-z]{3,12})/(\d{3,})/(\d{3,})(?=[/?#]|$)")


class ShopeeError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class ShopeeAuthError(ShopeeError):
    pass


ERRORS = {
    10020: "assinatura/credencial inválida (confira o AppID e a Senha)",
    10030: "limite de requisições da Shopee atingido — aumente o intervalo de busca",
    10031: "acesso negado",
    10032: "ID de afiliado inválido",
    10033: "conta de afiliado congelada",
    10034: "ID de afiliado bloqueado",
    10035: "sua conta não tem acesso à Open API (peça liberação à Shopee)",
    11001: "parâmetros inválidos",
}


def _money(value: Any) -> Decimal | None:
    try:
        v = Decimal(str(value)).quantize(CENT)
        return v if v > 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def parse_item_ids(url: str) -> tuple[int, int] | None:
    """(shopId, itemId) a partir da URL de produto da Shopee."""
    m = ITEM_RE.search(url or "")
    if not m:
        return None
    shop, item = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    return int(shop), int(item)


def _gql_str(value: str) -> str:
    """String literal GraphQL (o escape do JSON é compatível)."""
    return json.dumps(str(value)[:200], ensure_ascii=False)


def to_product(node: dict, source: str | None = None) -> Product | None:
    price = _money(node.get("priceMin")) or _money(node.get("priceMax"))
    if price is None or not node.get("itemId"):
        return None
    try:
        rate = int(node.get("priceDiscountRate") or 0)
    except (TypeError, ValueError):
        rate = 0
    original = None
    if 0 < rate < 100:  # a API não traz o preço "de": calcula pelo % de desconto
        original = (price / (Decimal(100 - rate) / 100)).quantize(CENT, rounding=ROUND_HALF_UP)
    image = node.get("imageUrl") or None
    if image and image.startswith("http://"):
        image = "https://" + image[len("http://"):]
    cats = node.get("productCatIds") or []
    return Product(
        external_id=f"{node.get('shopId')}.{node.get('itemId')}",
        title=str(node.get("productName") or "")[:300] or "Produto Shopee",
        url=node.get("productLink") or f"https://shopee.com.br/product/{node.get('shopId')}/{node.get('itemId')}",
        image=image,
        price=price,
        original_price=original if original and original > price else None,
        category_id=str(cats[-1]) if cats else None,
        free_shipping=False,
        source=source,
        raw=node,
        platform=PLATFORM,
        affiliate_url=node.get("offerLink") or None,
        commission_rate=_fraction(node.get("commissionRate")),
        rating=_rating(node.get("ratingStar")),
        sold=_int_or_none(node.get("sales")),
    )


def _fraction(value: Any) -> Decimal | None:
    try:
        v = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return v.quantize(Decimal("0.0001")) if 0 < v < 1 else None


def _rating(value: Any) -> Decimal | None:
    try:
        v = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return v if 0 < v <= 5 else None


def _int_or_none(value: Any) -> int | None:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v if v >= 0 else None


class ShopeeClient:
    def __init__(self, app_id: str, secret: str, *, timeout: float = 20.0,
                 transport: httpx.BaseTransport | None = None, clock=time.time):
        if not app_id or not secret:
            raise ShopeeAuthError("Informe o AppID e a Senha da API de afiliados da Shopee no painel.")
        self.app_id = app_id.strip()
        self._secret = secret.strip()
        self._clock = clock
        self._http = httpx.Client(timeout=timeout, transport=transport,
                                  headers={"Content-Type": "application/json", "User-Agent": "telegram-vendas/1.0"})

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ base
    def sign(self, timestamp: int, payload: str) -> str:
        return hashlib.sha256(f"{self.app_id}{timestamp}{payload}{self._secret}".encode()).hexdigest()

    def _query(self, query: str) -> dict:
        payload = json.dumps({"query": query}, separators=(",", ":"))
        ts = int(self._clock())
        auth = f"SHA256 Credential={self.app_id}, Timestamp={ts}, Signature={self.sign(ts, payload)}"
        try:
            r = self._http.post(API, content=payload, headers={"Authorization": auth})
        except httpx.HTTPError as exc:
            raise ShopeeError(f"Falha de conexão com a Shopee: {exc.__class__.__name__}") from exc
        try:
            data = r.json()
        except ValueError:
            raise ShopeeError(f"Resposta inválida da Shopee (HTTP {r.status_code})") from None
        errors = data.get("errors") or []
        if errors:
            err = errors[0]
            code = (err.get("extensions") or {}).get("code")
            msg = ERRORS.get(code) or err.get("message") or "erro desconhecido"
            cls = ShopeeAuthError if code in (10020, 10031, 10032, 10033, 10034, 10035) else ShopeeError
            raise cls(f"Shopee: {msg} (código {code})", code)
        if r.status_code >= 400:
            raise ShopeeError(f"Shopee respondeu HTTP {r.status_code}")
        return data.get("data") or {}

    # ------------------------------------------------------------ relatório
    def conversion_report(self, start_ts: int, end_ts: int, *, max_pages: int = 20) -> list[dict]:
        """Conversões (pedidos) feitas pelos SEUS links no período (unix). A API não informa cliques.

        Pagina pelo scrollId (vale ~30 s: as páginas seguintes são pedidas logo em seguida)."""
        fields = ("purchaseTime conversionId totalCommission utmContent "
                  "orders { orderId orderStatus items { itemId itemName qty itemTotalCommission } }")
        nodes: list[dict] = []
        scroll = ""
        for _ in range(max_pages):
            args = f"purchaseTimeStart: {int(start_ts)}, purchaseTimeEnd: {int(end_ts)}, limit: 500"
            if scroll:
                args += f", scrollId: {_gql_str(scroll)}"
            data = self._query(f"{{ conversionReport({args}) {{ nodes {{ {fields} }} "
                               f"pageInfo {{ hasNextPage scrollId }} }} }}")
            report = data.get("conversionReport") or {}
            nodes += report.get("nodes") or []
            page = report.get("pageInfo") or {}
            scroll = page.get("scrollId") or ""
            if not page.get("hasNextPage") or not scroll:
                break
        return nodes

    # --------------------------------------------------------------- fontes
    def offers(self, *, keyword: str | None = None, category_id: int | None = None, sort: str = "sales",
               limit: int = 20, page: int = 1, source: str | None = None) -> list[Product]:
        # valores escritos direto na consulta (strings com escape JSON, números validados)
        args = [f"sortType: {SORTS.get(sort, 2)}", f"limit: {max(1, min(int(limit), 50))}", f"page: {max(1, int(page))}"]
        if keyword:
            args.append(f"keyword: {_gql_str(keyword)}")
        if category_id:
            args.append(f"productCatId: {int(category_id)}")
        query = (f"{{ productOfferV2({', '.join(args)}) {{ nodes {{ {OFFER_FIELDS} }} "
                 "pageInfo { page limit hasNextPage } } }")
        nodes = ((self._query(query).get("productOfferV2") or {}).get("nodes")) or []
        return [p for p in (to_product(n, source) for n in nodes) if p]

    def item(self, shop_id: int, item_id: int, source: str | None = None) -> Product | None:
        query = (f"{{ productOfferV2(shopId: {int(shop_id)}, itemId: {int(item_id)}, limit: 1) "
                 f"{{ nodes {{ {OFFER_FIELDS} }} }} }}")
        nodes = ((self._query(query).get("productOfferV2") or {}).get("nodes")) or []
        return to_product(nodes[0], source) if nodes else None

    def short_link(self, url: str, sub_ids: list[str] | None = None) -> str:
        subs = ", ".join(_gql_str(x) for x in (sub_ids or [])[:5])
        query = (f"mutation {{ generateShortLink(input: {{originUrl: {_gql_str(url)}, subIds: [{subs}]}}) "
                 "{ shortLink } }")
        link = (self._query(query).get("generateShortLink") or {}).get("shortLink")
        if not link:
            raise ShopeeError("A Shopee não gerou o link de afiliado.")
        return link

    def test_connection(self) -> list[tuple[str, bool, str]]:
        steps: list[tuple[str, bool, str]] = []
        try:
            found = self.offers(keyword="fone bluetooth", limit=5)
            steps.append(("Autenticação (AppID + Senha)", True, "assinatura aceita"))
            steps.append(("Busca de ofertas", bool(found), f"{len(found)} produtos"))
        except ShopeeError as exc:
            steps.append(("Autenticação (AppID + Senha)", False, str(exc)))
            return steps
        if found:
            p = found[0]
            steps.append(("Link de afiliado", bool(p.affiliate_url),
                          f"{p.title[:50]} — R$ {p.price} · {p.affiliate_url or 'sem offerLink'}"))
        return steps
