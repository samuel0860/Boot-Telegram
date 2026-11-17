"""Modo conversor: o admin envia um link do Mercado Livre ao bot e recebe o
post pronto para publicar.

Aceita:
- link de afiliado curto (https://meli.la/...) — usado como link do botão;
- link de produto do catálogo (.../p/MLB123456);
- link de anúncio (produto.mercadolivre.com.br/MLB-123456-...).

Para links curtos, apenas lê os cabeçalhos de redirecionamento (Location)
até descobrir o ID do produto — a página do Mercado Livre não é baixada.
Os dados do produto vêm da API oficial (mesmas credenciais do bot de promoções).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx
from sqlalchemy import select

from database import session_scope
from models import OfferStatus, PromoOffer
from services import settings_service
from services.access_service import log_event, utcnow
from services.mercadolivre import MercadoLivreClient, MLError
from services.promo_service import PLATFORM, ClientFactory, caption_for, default_client_factory

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
PRODUCT_RE = re.compile(r"/p/(MLB\d{3,})", re.I)
ITEM_RE = re.compile(r"(MLB)-?(\d{6,})", re.I)
ML_HOSTS = ("mercadolivre.com.br", "mercadolibre.com", "mercadolivre.com", "meli.la")


class ConvertError(Exception):
    """Mensagem amigável para o administrador."""


def find_link(text: str) -> str | None:
    for url in URL_RE.findall(text or ""):
        host = urlsplit(url).hostname or ""
        if any(host == h or host.endswith("." + h) for h in ML_HOSTS):
            return url.rstrip(").,;")
    return None


def is_affiliate_link(url: str) -> bool:
    return (urlsplit(url).hostname or "").endswith("meli.la")


def extract_id(url: str) -> tuple[str, str] | None:
    """('PRODUCT'|'ITEM', 'MLB123') a partir de uma URL do Mercado Livre."""
    path = urlsplit(url).path
    m = PRODUCT_RE.search(path)
    if m:
        return "PRODUCT", m.group(1).upper()
    m = ITEM_RE.search(path) or ITEM_RE.search(url)
    if m:
        return "ITEM", f"MLB{m.group(2)}"
    return None


def resolve(url: str, *, transport: httpx.BaseTransport | None = None, max_hops: int = 6) -> tuple[str, str]:
    """Segue os redirecionamentos (só cabeçalhos) até achar o ID do produto."""
    found = extract_id(url)
    if found:
        return found
    current = url
    with httpx.Client(timeout=15, follow_redirects=False, transport=transport,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; telegram-vendas/1.0)"}) as client:
        for _ in range(max_hops):
            try:
                r = client.head(current)
                if r.status_code == 405:
                    r = client.get(current)
            except httpx.HTTPError as exc:
                raise ConvertError("Não consegui abrir esse link agora. Tente de novo em instantes.") from exc
            location = r.headers.get("location")
            if not location:
                break
            current = urljoin(current, location)
            found = extract_id(current)
            if found:
                return found
    raise ConvertError("Não identifiquei o produto nesse link. Envie o link da página do produto "
                       "(o endereço que aparece no navegador) ou um link meli.la.")


@dataclass(frozen=True)
class Preview:
    offer_id: int
    caption: str
    image_url: str | None
    link: str
    discount_pct: int | None


def import_link(text: str, *, client_factory: ClientFactory = default_client_factory,
                transport: httpx.BaseTransport | None = None, admin_telegram_id: int | None = None) -> Preview:
    url = find_link(text)
    if not url:
        raise ConvertError("Envie um link do Mercado Livre (meli.la ou da página do produto).")

    with session_scope() as session:
        values = settings_service.get_all(session)
    cfg = settings_service.promo_config(values)
    if not cfg.credentials_ok:
        raise ConvertError("Configure o App ID e a Secret Key do Mercado Livre no painel → Bot de promoções.")

    kind, ml_id = resolve(url, transport=transport)
    try:
        with client_factory(cfg) as client:
            product = client.product(ml_id, source="conversor") if kind == "PRODUCT" else client.item(ml_id, source="conversor")
    except MLError as exc:
        raise ConvertError(f"O Mercado Livre não respondeu: {exc}") from exc
    if product is None:
        raise ConvertError("Não consegui ler esse produto pela API do Mercado Livre (pode estar sem estoque "
                           "ou ser um anúncio restrito). Tente o link da página do produto no catálogo (/p/MLB...).")

    affiliate = url if is_affiliate_link(url) else None
    with session_scope() as session:
        offer = session.scalar(select(PromoOffer).where(
            PromoOffer.platform == PLATFORM, PromoOffer.external_id == product.external_id).with_for_update())
        if offer is None:
            offer = PromoOffer(platform=PLATFORM, external_id=product.external_id)
            session.add(offer)
        if offer.status in (None, OfferStatus.DISCARDED, OfferStatus.FAILED, OfferStatus.POSTED):
            offer.status = OfferStatus.NEW
        offer.title = product.title
        offer.product_url = product.url
        offer.image_url = product.image
        offer.price = product.price
        offer.original_price = product.original_price
        offer.discount_pct = product.discount_pct
        offer.free_shipping = product.free_shipping
        offer.category_id = product.category_id
        offer.source = "conversor"
        offer.last_seen_at = utcnow()
        if affiliate:
            offer.affiliate_url = affiliate
        session.flush()
        log_event(session, "converter_import", offer_id=offer.id, telegram_id=admin_telegram_id, url=url[:200])
        return Preview(offer.id, caption_for(offer, cfg.footer), offer.image_url, offer.link, offer.discount_pct)
