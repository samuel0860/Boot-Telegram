"""Cliente da API OFICIAL de afiliados da Amazon — Creators API (substituiu a PA-API 5.0 em 2026).

Credenciais (Associates Central → Ferramentas → Creators API → Criar credencial):
- Credential ID e Credential Secret;
- Versão da credencial (3.1 = Login with Amazon, a das contas novas; 2.1 = Cognito, contas antigas);
- Partner Tag = o seu ID de associado (ex.: meusite-20). É ele que garante a comissão.

Fluxo:
1. Token OAuth2 (client_credentials, escopo creatorsapi::default), válido ~1 h — fica em cache;
2. POST https://creatorsapi.amazon/catalog/v1/searchItems | getItems
   com Authorization: Bearer <token> e x-marketplace: www.amazon.com.br.

O detailPageURL devolvido já vem com o seu Partner Tag: é o link de afiliado.

Observação: a Amazon só libera a Creators API para associados com vendas
qualificadas recentes. Sem isso, a autenticação ou a busca é recusada.
"""
from __future__ import annotations

import re
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from offers.mercadolivre import Product

API = "https://creatorsapi.amazon/catalog/v1"
MARKETPLACE = "www.amazon.com.br"
PLATFORM = "amazon"
CENT = Decimal("0.01")

TOKEN_ENDPOINTS = {
    "2.1": "https://creatorsapi.auth.us-east-1.amazoncognito.com/oauth2/token",
    "3.1": "https://api.amazon.com/auth/o2/token",
}
VERSIONS = {"3.1": "3.1 (credencial nova)", "2.1": "2.1 (credencial antiga)"}

SORTS = {"featured": "Featured", "relevance": "Relevance", "price_asc": "Price:LowToHigh",
         "reviews": "AvgCustomerReviews", "newest": "NewestArrivals"}
SORT_LABELS = {"featured": "Destaques", "relevance": "Relevância", "price_asc": "Menor preço",
               "reviews": "Melhor avaliados", "newest": "Novidades"}

RESOURCES = [
    "itemInfo.title",
    "images.primary.large",
    "offersV2.listings.price",
    "offersV2.listings.isBuyBoxWinner",
    "offersV2.listings.merchantInfo",
    "browseNodeInfo.browseNodes",
]

# https://www.amazon.com.br/Nome/dp/B0ABCDE123 | /gp/product/B0ABCDE123 | /gp/aw/d/B0ABCDE123
ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d|d|exec/obidos/asin)/([A-Z0-9]{10})(?:[/?#]|$)", re.I)
TAG_RE = re.compile(r"^[A-Za-z0-9-]{2,40}-2\d$")


class AmazonError(Exception):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class AmazonAuthError(AmazonError):
    pass


ERRORS = {
    "InvalidPartnerTag": "Partner Tag inválido (confira o seu ID de associado, ex.: meusite-20)",
    "UnrecognizedClient": "credencial não reconhecida (confira o Credential ID e o Secret)",
    "InvalidSignature": "credencial inválida",
    "AccessDenied": "acesso negado — a Amazon só libera a Creators API para associados com vendas recentes",
    "AccessDeniedException": "acesso negado — a Amazon só libera a Creators API para associados com vendas recentes",
    "AssociateNotEligible": "sua conta de associado ainda não está habilitada para a Creators API",
    "TooManyRequests": "limite de requisições da Amazon atingido — aumente o intervalo de busca",
    "ThrottlingException": "limite de requisições da Amazon atingido — aumente o intervalo de busca",
    "NoResults": "nenhum resultado",
    "ItemNotAccessible": "produto indisponível pela API",
    "InvalidParameterValue": "parâmetro inválido",
}
AUTH_CODES = {"InvalidPartnerTag", "UnrecognizedClient", "InvalidSignature", "AccessDenied",
              "AccessDeniedException", "AssociateNotEligible"}


def valid_tag(tag: str) -> bool:
    """IDs de associado do Brasil terminam em -20 (ex.: meusite-20)."""
    return bool(TAG_RE.fullmatch((tag or "").strip()))


def parse_asin(url: str) -> str | None:
    m = ASIN_RE.search(urlsplit(url or "").path + "/")
    return m.group(1).upper() if m else None


def with_tag(url: str, tag: str) -> str:
    """Garante o seu Partner Tag no link (substitui o de outra pessoa)."""
    if not tag:
        return url
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "tag"]
    query.append(("tag", tag))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _money(value: Any) -> Decimal | None:
    try:
        v = Decimal(str(value)).quantize(CENT)
        return v if v > 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _get(d: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def _main_listing(item: dict) -> dict | None:
    listings = _get(item, "offersV2", "listings") or []
    if not listings:
        return None
    return next((x for x in listings if x.get("isBuyBoxWinner")), listings[0])


def to_product(item: dict, tag: str = "", source: str | None = None) -> Product | None:
    asin = item.get("asin")
    listing = _main_listing(item)
    price = _money(_get(listing, "price", "money", "amount"))
    if not asin or price is None:
        return None
    original = _money(_get(listing, "price", "savingBasis", "money", "amount"))
    if original is None:
        saved = _money(_get(listing, "price", "savings", "money", "amount"))
        if saved:
            original = price + saved
        else:
            try:
                pct = int(_get(listing, "price", "savings", "percentage") or 0)
            except (TypeError, ValueError):
                pct = 0
            if 0 < pct < 100:
                original = (price / (Decimal(100 - pct) / 100)).quantize(CENT, rounding=ROUND_HALF_UP)
    url = item.get("detailPageURL") or f"https://{MARKETPLACE}/dp/{asin}"
    nodes = _get(item, "browseNodeInfo", "browseNodes") or []
    return Product(
        external_id=str(asin),
        title=str(_get(item, "itemInfo", "title", "displayValue") or "")[:300] or "Produto Amazon",
        url=f"https://{MARKETPLACE}/dp/{asin}",
        image=_get(item, "images", "primary", "large", "url") or _get(item, "images", "primary", "medium", "url"),
        price=price,
        original_price=original if original and original > price else None,
        category_id=str(nodes[0].get("id")) if nodes and isinstance(nodes[0], dict) and nodes[0].get("id") else None,
        free_shipping=False,
        source=source,
        raw=item,
        platform=PLATFORM,
        affiliate_url=with_tag(url, tag),
    )


class AmazonClient:
    def __init__(self, credential_id: str, secret: str, partner_tag: str, version: str = "3.1", *,
                 timeout: float = 20.0, transport: httpx.BaseTransport | None = None, clock=time.time):
        if not credential_id or not secret or not partner_tag:
            raise AmazonAuthError("Informe o Credential ID, o Secret e o Partner Tag da Amazon no painel.")
        self.credential_id = credential_id.strip()
        self._secret = secret.strip()
        self.tag = partner_tag.strip()
        self.version = version if version in TOKEN_ENDPOINTS else "3.1"
        self._clock = clock
        self._token: str | None = None
        self._expires = 0.0
        self._http = httpx.Client(timeout=timeout, transport=transport,
                                  headers={"User-Agent": "telegram-vendas/1.0"})

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ auth
    @property
    def lwa(self) -> bool:
        return self.version.startswith("3")

    def token(self) -> str:
        if self._token and self._clock() < self._expires:
            return self._token
        data = {"grant_type": "client_credentials", "client_id": self.credential_id,
                "client_secret": self._secret, "scope": "creatorsapi::default" if self.lwa else "creatorsapi/default"}
        try:
            if self.lwa:
                r = self._http.post(TOKEN_ENDPOINTS[self.version], json=data)
            else:
                r = self._http.post(TOKEN_ENDPOINTS[self.version], data=data)
        except httpx.HTTPError as exc:
            raise AmazonError(f"Falha de conexão com a Amazon: {exc.__class__.__name__}") from exc
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code >= 400 or not body.get("access_token"):
            detail = body.get("error_description") or body.get("error") or f"HTTP {r.status_code}"
            raise AmazonAuthError(f"Amazon: credencial recusada ({detail}). Confira o Credential ID, o Secret "
                                  "e a versão da credencial.")
        self._token = str(body["access_token"])
        self._expires = self._clock() + int(body.get("expires_in") or 3600) - 60
        return self._token

    def _auth_header(self) -> str:
        token = self.token()
        return f"Bearer {token}" if self.lwa else f"Bearer {token}, Version {self.version}"

    def _post(self, operation: str, payload: dict) -> dict:
        body = {"partnerTag": self.tag, "resources": RESOURCES, **payload}
        headers = {"Authorization": self._auth_header(), "Content-Type": "application/json",
                   "x-marketplace": MARKETPLACE}
        try:
            r = self._http.post(f"{API}/{operation}", json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise AmazonError(f"Falha de conexão com a Amazon: {exc.__class__.__name__}") from exc
        try:
            data = r.json()
        except ValueError:
            data = {}
        errors = data.get("errors") or data.get("Errors") or []
        if r.status_code >= 400 or (errors and not (data.get("searchResult") or data.get("itemsResult"))):
            err = errors[0] if errors else {}
            code = err.get("code") or err.get("Code") or data.get("__type", "").split("#")[-1] or None
            if code == "NoResults":
                return {}
            msg = ERRORS.get(code or "") or err.get("message") or err.get("Message") or data.get("message") \
                or f"HTTP {r.status_code}"
            if r.status_code in (401, 403) or code in AUTH_CODES:
                raise AmazonAuthError(f"Amazon: {msg}" + (f" (código {code})" if code else ""), code)
            raise AmazonError(f"Amazon: {msg}" + (f" (código {code})" if code else ""), code)
        return data

    # --------------------------------------------------------------- fontes
    def search(self, *, keywords: str | None = None, browse_node_id: str | None = None, sort: str = "featured",
               min_saving: int = 0, page: int = 1, source: str | None = None) -> list[Product]:
        payload: dict[str, Any] = {"itemCount": 10, "itemPage": max(1, min(int(page), 10)), "searchIndex": "All"}
        if keywords:
            payload["keywords"] = str(keywords)[:200]
        if browse_node_id:
            payload["browseNodeId"] = str(browse_node_id)
        if sort in SORTS:
            payload["sortBy"] = SORTS[sort]
        if 0 < int(min_saving) < 100:
            payload["minSavingPercent"] = int(min_saving)
        items = _get(self._post("searchItems", payload), "searchResult", "items") or []
        return [p for p in (to_product(i, self.tag, source) for i in items) if p]

    def item(self, asin: str, source: str | None = None) -> Product | None:
        items = _get(self._post("getItems", {"itemIds": [asin]}), "itemsResult", "items") or []
        return to_product(items[0], self.tag, source) if items else None

    def test_connection(self) -> list[tuple[str, bool, str]]:
        steps: list[tuple[str, bool, str]] = []
        try:
            self.token()
            steps.append(("Autenticação (Credential ID + Secret)", True, f"token emitido · versão {self.version}"))
        except AmazonError as exc:
            steps.append(("Autenticação (Credential ID + Secret)", False, str(exc)))
            return steps
        try:
            found = self.search(keywords="fone bluetooth")
        except AmazonError as exc:
            steps.append(("Busca de ofertas", False, str(exc)))
            return steps
        steps.append(("Busca de ofertas", bool(found), f"{len(found)} produtos"))
        if found:
            p = found[0]
            steps.append(("Link de afiliado", f"tag={self.tag}" in (p.affiliate_url or ""),
                          f"{p.title[:50]} — R$ {p.price} · {p.affiliate_url}"))
        return steps
