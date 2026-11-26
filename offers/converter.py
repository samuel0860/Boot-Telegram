"""Modo conversor: o admin envia um link do Mercado Livre OU da Shopee ao bot de
ofertas e recebe o post pronto para publicar.

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

from core import tenancy
from core.audit import log_event, utcnow
from database import session_scope
from models import OfferBot, OfferStatus
from offers import amazon, collector, shopee, whatsapp
from offers import bots as bot_service
from offers.collector import (
    AmazonFactory,
    ClientFactory,
    ShopeeFactory,
    default_amazon_factory,
    default_client_factory,
    default_shopee_factory,
)
from offers.mercadolivre import MLError
from offers.posting import telegram_caption

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
PRODUCT_RE = re.compile(r"/p/(MLB\d{3,})", re.I)
ITEM_RE = re.compile(r"(MLB)-?(\d{6,})", re.I)
ML_HOSTS = ("mercadolivre.com.br", "mercadolibre.com", "mercadolivre.com", "meli.la")
SHOPEE_HOSTS = ("shopee.com.br", "shp.ee", "shope.ee")
SHOPEE_SHORT_HOSTS = ("s.shopee.com.br", "shp.ee", "shope.ee")   # links curtos = afiliado de quem gerou
AMAZON_HOSTS = ("amazon.com.br", "amzn.to", "a.co", "amzn.com")
AMAZON_SHORT_HOSTS = ("amzn.to", "a.co")


COUPON_RE = re.compile(r"^\s*(?:⚠️\s*)?cupo[mn]\s*[:=-]\s*(.+?)\s*$", re.I | re.M)
HEADLINE_RE = re.compile(r"^\s*(?:chamada|t[íi]tulo)\s*[:=-]\s*(.+?)\s*$", re.I | re.M)


def parse_extras(text: str) -> tuple[str | None, str | None]:
    """Lê 'chamada: ...' e 'cupom: ...' da mensagem enviada ao bot."""
    headline = HEADLINE_RE.search(text or "")
    coupon = COUPON_RE.search(text or "")
    return (headline.group(1)[:120] if headline else None,
            coupon.group(1).strip("*_~ ")[:40] if coupon else None)


class ConvertError(Exception):
    """Mensagem amigável para o administrador."""


def _host_in(host: str, hosts: tuple[str, ...]) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def store_of(url: str) -> str | None:
    """'mercadolivre', 'shopee', 'amazon' ou None."""
    host = urlsplit(url).hostname or ""
    if _host_in(host, ML_HOSTS):
        return "mercadolivre"
    if _host_in(host, SHOPEE_HOSTS):
        return "shopee"
    if _host_in(host, AMAZON_HOSTS):
        return "amazon"
    return None


def find_link(text: str) -> str | None:
    """Primeiro link do Mercado Livre, da Shopee ou da Amazon na mensagem."""
    for url in URL_RE.findall(text or ""):
        if store_of(url):
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


def _follow(url: str, extract, transport: httpx.BaseTransport | None, max_hops: int):
    """Segue os redirecionamentos (só cabeçalhos, sem baixar a página) até `extract` achar o produto."""
    found = extract(url)
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
            found = extract(current)
            if found:
                return found
    return None


def resolve(url: str, *, transport: httpx.BaseTransport | None = None, max_hops: int = 6) -> tuple[str, str]:
    """Mercado Livre: ('PRODUCT'|'ITEM', 'MLB...')."""
    found = _follow(url, extract_id, transport, max_hops)
    if found:
        return found
    raise ConvertError("Não identifiquei o produto nesse link. Envie o link da página do produto "
                       "(o endereço que aparece no navegador) ou um link meli.la.")


def resolve_shopee(url: str, *, transport: httpx.BaseTransport | None = None, max_hops: int = 6) -> tuple[int, int]:
    """Shopee: (shopId, itemId)."""
    found = _follow(url, shopee.parse_item_ids, transport, max_hops)
    if found:
        return found
    raise ConvertError("Não identifiquei o produto nesse link da Shopee. Envie o link da página do produto "
                       "ou o seu link curto (s.shopee.com.br/...).")


def resolve_amazon(url: str, *, transport: httpx.BaseTransport | None = None, max_hops: int = 6) -> str:
    """Amazon: ASIN do produto (segue amzn.to / a.co)."""
    found = _follow(url, amazon.parse_asin, transport, max_hops)
    if found:
        return found
    raise ConvertError("Não identifiquei o produto nesse link da Amazon. Envie o link da página do produto "
                       "(amazon.com.br/.../dp/...) ou um link amzn.to.")


@dataclass(frozen=True)
class Preview:
    offer_id: int
    bot_id: int
    caption: str
    image_url: str | None
    link: str
    discount_pct: int | None
    has_extras: bool = False
    has_whatsapp: bool = False


def import_link(bot_id: int, text: str, **kwargs) -> Preview:
    """Lê o produto do link e prepara o post NESTE bot — sempre dentro da conta dona do bot."""
    with session_scope() as session:
        bot = session.get(OfferBot, bot_id)
        account_id = bot.account_id if bot is not None else tenancy.current_account_id()
    with tenancy.use_account(account_id):
        return _import_link(bot_id, text, **kwargs)


def _import_link(bot_id: int, text: str, *, client_factory: ClientFactory = default_client_factory,
                shopee_factory: ShopeeFactory = default_shopee_factory,
                amazon_factory: AmazonFactory = default_amazon_factory,
                transport: httpx.BaseTransport | None = None, admin_telegram_id: int | None = None) -> Preview:
    """Lê o produto do link (Mercado Livre, Shopee ou Amazon) e prepara o post NESTE bot (ainda não publica)."""
    url = find_link(text)
    if not url:
        raise ConvertError("Envie um link do Mercado Livre (meli.la ou da página do produto), da Shopee ou da Amazon.")

    with session_scope() as session:
        bot = session.get(OfferBot, bot_id)
        if bot is None:
            raise ConvertError("Bot de ofertas não encontrado.")
        cfg = bot_service.config_for(session, bot)

    affiliate = None
    store = store_of(url)
    if store == "amazon":
        if not cfg.amazon_credentials_ok:
            raise ConvertError("Configure o Credential ID, o Secret e o Partner Tag da Amazon no painel → "
                               "Ofertas → Lojas e APIs.")
        asin = resolve_amazon(url, transport=transport)
        try:
            with amazon_factory(cfg) as client:
                product = client.item(asin, source="conversor")
        except amazon.AmazonError as exc:
            raise ConvertError(str(exc)) from exc
        if product is None:
            raise ConvertError("A Amazon não devolveu esse produto (pode estar sem estoque ou sem oferta ativa).")
        host = urlsplit(url).hostname or ""
        # link curto que você mesmo gerou (SiteStripe) já leva a sua tag; senão usa o da API, com a sua tag
        affiliate = url if _host_in(host, AMAZON_SHORT_HOSTS) else product.affiliate_url
    elif store == "shopee":
        if not cfg.shopee_credentials_ok:
            raise ConvertError("Configure o AppID e a Senha da Shopee no painel → Ofertas → Lojas e APIs.")
        shop_id, item_id = resolve_shopee(url, transport=transport)
        try:
            with shopee_factory(cfg) as client:
                product = client.item(shop_id, item_id, source="conversor")
                if product is None:
                    raise ConvertError("A Shopee não devolveu esse produto (pode estar fora do programa de afiliados).")
                host = urlsplit(url).hostname or ""
                if _host_in(host, SHOPEE_SHORT_HOSTS):
                    affiliate = url                      # o link curto que você mesmo gerou
                else:
                    affiliate = product.affiliate_url or client.short_link(product.url, ["telegram"])
        except shopee.ShopeeError as exc:
            raise ConvertError(str(exc)) from exc
    else:
        if not cfg.credentials_ok:
            raise ConvertError("Configure o App ID e a Secret Key do Mercado Livre no painel → Ofertas → Lojas e APIs.")
        kind, ml_id = resolve(url, transport=transport)
        try:
            with client_factory(cfg) as client:
                product = (client.product(ml_id, source="conversor") if kind == "PRODUCT"
                           else client.item(ml_id, source="conversor"))
        except MLError as exc:
            raise ConvertError(f"O Mercado Livre não respondeu: {exc}") from exc
        if product is None:
            raise ConvertError("Não consegui ler esse produto pela API do Mercado Livre (pode estar sem estoque "
                               "ou ser um anúncio restrito). Tente o link da página do produto no catálogo (/p/MLB...).")
        affiliate = url if is_affiliate_link(url) else None

    headline, coupon = parse_extras(text)
    with session_scope() as session:
        offer, _ = collector.upsert_offer(session, product, utcnow())
        offer.source = "conversor"
        if affiliate:
            offer.affiliate_url = affiliate
        # chamada e cupom valem para este post (o que não foi informado é limpo)
        offer.headline = headline
        offer.coupon = coupon
        post, _ = collector.post_for(session, offer.id, bot_id)
        if post.status in (OfferStatus.DISCARDED, OfferStatus.FAILED, OfferStatus.POSTED):
            post.status = OfferStatus.NEW
        bot = session.get(OfferBot, bot_id)
        has_wa = whatsapp.wa_config(bot, cfg).ready
        log_event(session, "converter_import", offer_id=offer.id, bot_id=bot_id, telegram_id=admin_telegram_id,
                  url=url[:200], store=product.platform)
        return Preview(offer.id, bot_id, telegram_caption(offer, cfg), offer.image_url, offer.link,
                       offer.discount_pct, bool(headline or coupon), has_wa)
