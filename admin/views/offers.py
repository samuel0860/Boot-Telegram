"""MÓDULO DE OFERTAS no painel: bots de ofertas, destinos (Telegram/WhatsApp),
busca, modelo do post, ofertas de cada bot e as contas das lojas (Mercado Livre, Shopee e Amazon)."""
from __future__ import annotations

import re
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from admin.auth import client_ip, current_admin, require_superuser, verify_csrf
from admin.common import audit, flash, paginate, redirect, render, valid_affiliate
from config import settings
from core import alert_service, settings_service
from core.alert_service import OFFERS_HEARTBEAT
from core.audit import utcnow
from database import get_db
from models import AdminUser, OfferBot, OfferPost, OfferStatus, OfferTarget, PromoOffer, TargetChannel
from offers import amazon, collector, converter, overview, post_template, whatsapp
from offers import bots as bot_service
from offers import config as offer_config
from offers import stats as offer_stats
from offers.amazon import AmazonError
from offers.mercadolivre import MLError
from offers.posting import telegram_caption
from offers.shopee import SORT_LABELS, ShopeeError
from offers.whatsapp import WhatsAppError

router = APIRouter()

# Podem ser substituídas nos testes
ml_client_factory = collector.default_client_factory
shopee_client_factory = collector.default_shopee_factory
amazon_client_factory = collector.default_amazon_factory
wa_client_factory = whatsapp.default_client
telegram_get_me = bot_service.telegram_get_me

TABS = {"geral": "Geral", "telegram": "Telegram", "whatsapp": "WhatsApp", "busca": "Busca e filtros",
        "post": "Modelo do post"}
SAMPLE_POST = post_template.PostData(
    title='Smart TV Philco 43" Roku Tv Hdr10', price=Decimal("1095"), original_price=Decimal("1499"),
    discount_pct=27, link="https://meli.la/33hYTSb", coupon="DIADATECH", store="Mercado Livre",
)


def _bot(db: Session, bot_id: int, lock: bool = False) -> OfferBot:
    bot = db.get(OfferBot, bot_id, with_for_update=lock)
    if bot is None:
        raise HTTPException(404)
    return bot


def _back(bot_id: int, tab: str = "geral") -> str:
    return f"/admin/offers/bots/{bot_id}?tab={tab}"


# --------------------------------------------------------------------------- #
# Lista de bots
# --------------------------------------------------------------------------- #
@router.get("/offers/bots", response_class=HTMLResponse)
def bots_list(request: Request, admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    """Central dos bots de promoção: situação de todos os bots numa tela só."""
    bots = db.scalars(select(OfferBot).options(selectinload(OfferBot.targets)).order_by(OfferBot.id)).all()
    values = settings_service.get_all(db)
    worker_online = alert_service.bot_online(values, key=OFFERS_HEARTBEAT)
    now = utcnow()
    cards = [overview.build(b, bot_service.config_for(db, b), collector.stats(b.id), worker_online, now)
             for b in bots]
    totals = {
        "active": sum(1 for c in cards if c.bot.is_active),
        "posted_24h": sum(c.stats["posted_24h"] for c in cards),
        "wa_24h": sum(c.stats["wa_24h"] for c in cards),
        "queued": sum(c.stats["queued"] for c in cards),
        "attention": sum(1 for c in cards if c.level in ("warn", "setup")),
        "groups": sum(c.tg_count + c.wa_count for c in cards),
    }
    return render(request, "admin/offers_bots.html", admin, cards=cards, totals=totals,
                  worker_online=worker_online,
                  paused_all=any((b.state or {}).get("paused_by_all") for b in bots),
                  credentials_ok=bool((values.get("ml_app_id") and values.get("ml_secret_key"))
                                      or (values.get("shopee_app_id") and values.get("shopee_secret"))
                                      or (values.get("amazon_credential_id") and values.get("amazon_secret"))),
                  sales_bot=settings.telegram_bot_username)


@router.post("/offers/bots/pause-all")
def bots_pause_all(request: Request, _csrf: None = Depends(verify_csrf),
                   admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    bots = db.scalars(select(OfferBot).where(OfferBot.is_active.is_(True)).with_for_update()).all()
    for b in bots:
        b.is_active = False
        b.state = {**(b.state or {}), "paused_by_all": True}
    audit(db, request, admin, "offer_bots_paused_all", count=len(bots))
    db.commit()
    flash(request, f"{len(bots)} bot(s) pausado(s). Nada será publicado até você retomar.", "warn" if bots else "ok")
    return redirect("/admin/offers/bots")


@router.post("/offers/bots/resume-all")
def bots_resume_all(request: Request, _csrf: None = Depends(verify_csrf),
                    admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    bots = db.scalars(select(OfferBot).options(selectinload(OfferBot.targets)).with_for_update()).all()
    resumed = 0
    for b in bots:
        if not (b.state or {}).get("paused_by_all"):
            continue
        b.state = {k: v for k, v in (b.state or {}).items() if k != "paused_by_all"}
        if any(t.is_active for t in b.targets):
            b.is_active = True
            resumed += 1
    audit(db, request, admin, "offer_bots_resumed_all", count=resumed)
    db.commit()
    flash(request, f"{resumed} bot(s) religado(s).")
    return redirect("/admin/offers/bots")


@router.post("/offers/bots/create")
def bot_create(request: Request, name: str = Form(..., max_length=80), _csrf: None = Depends(verify_csrf),
               admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    try:
        bot = bot_service.create_bot(db, name)
    except bot_service.BotError as exc:
        flash(request, str(exc), "error")
        return redirect("/admin/offers/bots")
    audit(db, request, admin, "offer_bot_created", bot_id=bot.id, name=bot.name)
    db.commit()
    flash(request, f"Bot “{bot.name}” criado. Agora cadastre o token do Telegram e/ou conecte o WhatsApp.")
    return redirect(_back(bot.id))


# --------------------------------------------------------------------------- #
# Página do bot
# --------------------------------------------------------------------------- #
@router.get("/offers/bots/{bot_id}", response_class=HTMLResponse)
def bot_page(bot_id: int, request: Request, tab: str = "geral", qr: int = 0,
             admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    bot = _bot(db, bot_id)
    tab = tab if tab in TABS else "geral"
    cfg = bot_service.config_for(db, bot)
    values = offer_config.merged(bot.config)
    ctx = {"bot": bot, "cfg": cfg, "values": values, "tab": tab, "tabs": TABS,
           "stats": collector.stats(bot.id), "last": collector.last_result(bot),
           "tg_targets": [t for t in bot.targets if t.channel == TargetChannel.TELEGRAM],
           "wa_targets": [t for t in bot.targets if t.channel == TargetChannel.WHATSAPP],
           "credentials_ok": cfg.sources_ready, "test_results": request.session.pop("offers_test", None),
           "other_bots": db.scalars(select(OfferBot).where(OfferBot.id != bot.id).order_by(OfferBot.id)).all(),
           "copy_groups": {k: label for k, (label, _) in bot_service.COPY_GROUPS.items()}}
    if tab == "telegram":
        known = {t.target_id for t in bot.targets if t.channel == TargetChannel.TELEGRAM}
        ctx["seen_chats"] = [c for c in (bot.state or {}).get("seen_chats", []) if str(c.get("id")) not in known]
    elif tab == "whatsapp":
        ctx.update(_whatsapp_context(bot, cfg, admin, bool(qr)))
    elif tab == "busca":
        ctx.update(shopee_sorts=SORT_LABELS, amazon_sorts=amazon.SORT_LABELS, categories=offer_config.ML_CATEGORIES, selected_categories=set(cfg.categories),
                   extra_categories=", ".join(c for c in cfg.categories if c not in offer_config.ML_CATEGORIES))
    elif tab == "post":
        ctx.update(placeholders=post_template.PLACEHOLDERS,
                   template_preview=post_template.render_telegram(SAMPLE_POST, cfg.template, cfg.default_headline,
                                                                  cfg.footer))
    return render(request, "admin/offer_bot.html", admin, **ctx)


@router.post("/offers/bots/{bot_id}/general")
def bot_general(bot_id: int, request: Request, name: str = Form(..., max_length=80),
                _csrf: None = Depends(verify_csrf), admin: AdminUser = Depends(require_superuser),
                db: Session = Depends(get_db)):
    bot = _bot(db, bot_id, lock=True)
    if not name.strip():
        flash(request, "O nome não pode ficar vazio.", "error")
        return redirect(_back(bot_id))
    bot.name = name.strip()[:80]
    audit(db, request, admin, "offer_bot_renamed", bot_id=bot.id, name=bot.name)
    db.commit()
    flash(request, "Nome salvo.")
    return redirect(_back(bot_id))


@router.post("/offers/bots/{bot_id}/copy")
def bot_copy(bot_id: int, request: Request, source_id: int = Form(...), groups: list[str] = Form(default=[]),
             _csrf: None = Depends(verify_csrf), admin: AdminUser = Depends(require_superuser),
             db: Session = Depends(get_db)):
    bot = _bot(db, bot_id, lock=True)
    source = _bot(db, source_id)
    if source.id == bot.id or not groups:
        flash(request, "Escolha outro bot e pelo menos uma parte para copiar.", "error")
        return redirect(_back(bot_id))
    copied = bot_service.copy_config(source, bot, groups)
    audit(db, request, admin, "offer_bot_config_copied", bot_id=bot.id, source_id=source.id, parts=copied)
    db.commit()
    flash(request, f"Copiado de “{source.name}”: {', '.join(copied)}.")
    return redirect(_back(bot_id))


@router.post("/offers/bots/{bot_id}/toggle")
def bot_toggle(bot_id: int, request: Request, back: str = Form(""), _csrf: None = Depends(verify_csrf),
               admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    bot = _bot(db, bot_id, lock=True)
    dest = "/admin/offers/bots" if back == "central" else _back(bot_id)
    if not bot.is_active and not any(t.is_active for t in bot.targets):
        flash(request, "Antes de ligar, cadastre pelo menos um destino (canal do Telegram ou grupo do WhatsApp).",
              "error")
        return redirect(dest)
    bot.is_active = not bot.is_active
    bot.state = {k: v for k, v in (bot.state or {}).items() if k != "paused_by_all"}
    audit(db, request, admin, "offer_bot_toggled", bot_id=bot.id, active=bot.is_active)
    db.commit()
    flash(request, f"“{bot.name}” LIGADO." if bot.is_active else f"“{bot.name}” desligado.")
    return redirect(dest)


@router.post("/offers/bots/{bot_id}/token")
def bot_token(bot_id: int, request: Request, token: str = Form(..., max_length=100),
              _csrf: None = Depends(verify_csrf), admin: AdminUser = Depends(require_superuser),
              db: Session = Depends(get_db)):
    bot = _bot(db, bot_id, lock=True)
    token = token.strip()
    try:
        me = telegram_get_me(token)
        bot_service.set_token(db, bot, token, me)
    except bot_service.BotError as exc:
        flash(request, str(exc), "error")
        return redirect(_back(bot_id, "telegram"))
    audit(db, request, admin, "offer_bot_token_set", bot_id=bot.id, username=me.username)
    db.commit()
    flash(request, f"Token salvo: o bot é o @{me.username}. Em até 10 segundos ele fica online.")
    return redirect(_back(bot_id, "telegram"))


@router.post("/offers/bots/{bot_id}/token/remove")
def bot_token_remove(bot_id: int, request: Request, _csrf: None = Depends(verify_csrf),
                     admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    bot = _bot(db, bot_id, lock=True)
    bot_service.clear_token(bot)
    audit(db, request, admin, "offer_bot_token_removed", bot_id=bot.id)
    db.commit()
    flash(request, "Token removido. O Telegram deste bot foi desligado.", "warn")
    return redirect(_back(bot_id, "telegram"))


@router.post("/offers/bots/{bot_id}/delete")
def bot_delete(bot_id: int, request: Request, confirm_name: str = Form("", max_length=80),
               _csrf: None = Depends(verify_csrf), admin: AdminUser = Depends(require_superuser),
               db: Session = Depends(get_db)):
    bot = _bot(db, bot_id, lock=True)
    if confirm_name.strip() != bot.name:
        flash(request, "Para excluir, digite o nome do bot exatamente como está.", "error")
        return redirect(_back(bot_id))
    instance = bot.wa_instance
    name = bot.name
    db.delete(bot)
    audit(db, request, admin, "offer_bot_deleted", bot_id=bot_id, name=name)
    db.commit()
    if instance:  # desconecta o número (se estiver conectado)
        try:
            with wa_client_factory() as client:
                if client.state(instance) not in ("missing",):
                    client.logout(instance)
        except WhatsAppError:
            pass
    flash(request, f"Bot “{name}” excluído.", "warn")
    return redirect("/admin/offers/bots")


# --------------------------------------------------------------------------- #
# Destinos do Telegram
# --------------------------------------------------------------------------- #
@router.post("/offers/bots/{bot_id}/targets/add")
def target_add(bot_id: int, request: Request, target_id: str = Form(..., max_length=64),
               name: str = Form("", max_length=120), _csrf: None = Depends(verify_csrf),
               admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    bot = _bot(db, bot_id, lock=True)
    try:
        target = bot_service.add_target(db, bot, TargetChannel.TELEGRAM, target_id, name)
    except bot_service.BotError as exc:
        flash(request, str(exc), "error")
        return redirect(_back(bot_id, "telegram"))
    audit(db, request, admin, "offer_target_added", bot_id=bot.id, target=target.target_id)
    db.commit()
    flash(request, f"Destino “{target.name}” adicionado.")
    return redirect(_back(bot_id, "telegram"))


@router.post("/offers/bots/{bot_id}/targets/{target_id}/toggle")
def target_toggle(bot_id: int, target_id: int, request: Request, _csrf: None = Depends(verify_csrf),
                  admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    target = db.get(OfferTarget, target_id)
    if target is None or target.bot_id != bot_id:
        raise HTTPException(404)
    target.is_active = not target.is_active
    audit(db, request, admin, "offer_target_toggled", bot_id=bot_id, target=target.target_id, active=target.is_active)
    db.commit()
    return redirect(_back(bot_id, target.channel.value))


@router.post("/offers/bots/{bot_id}/targets/{target_id}/remove")
def target_remove(bot_id: int, target_id: int, request: Request, _csrf: None = Depends(verify_csrf),
                  admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    target = db.get(OfferTarget, target_id)
    if target is None or target.bot_id != bot_id:
        raise HTTPException(404)
    tab = target.channel.value
    db.delete(target)
    audit(db, request, admin, "offer_target_removed", bot_id=bot_id, target=target.target_id)
    db.commit()
    flash(request, "Destino removido.")
    return redirect(_back(bot_id, tab))


# --------------------------------------------------------------------------- #
# Busca/filtros e modelo do post
# --------------------------------------------------------------------------- #
@router.post("/offers/bots/{bot_id}/search")
def bot_search(
    bot_id: int,
    request: Request,
    mode: str = Form("auto"),
    interval_minutes: str = Form("45", max_length=5),
    max_posts_per_cycle: str = Form("3", max_length=3),
    spacing_seconds: str = Form("120", max_length=5),
    min_discount: str = Form("20", max_length=3),
    min_price: str = Form("0", max_length=12),
    max_price: str = Form("0", max_length=12),
    blocked_words: str = Form("", max_length=2000),
    categories: list[str] = Form(default=[]),
    extra_categories: str = Form("", max_length=500),
    keywords: str = Form("", max_length=1000),
    no_repeat_days: str = Form("7", max_length=4),
    active_hours: str = Form("", max_length=20),
    src_mercadolivre: str = Form(""),
    src_shopee: str = Form(""),
    shopee_keywords: str = Form("", max_length=1000),
    shopee_categories: str = Form("", max_length=300),
    shopee_sort: str = Form("sales", max_length=20),
    src_amazon: str = Form(""),
    amazon_keywords: str = Form("", max_length=1000),
    amazon_nodes: str = Form("", max_length=300),
    amazon_sort: str = Form("featured", max_length=20),
    skip_fake: str = Form(""),
    _csrf: None = Depends(verify_csrf),
    admin: AdminUser = Depends(require_superuser),
    db: Session = Depends(get_db),
):
    bot = _bot(db, bot_id, lock=True)
    errors = []
    if not src_mercadolivre and not src_shopee and not src_amazon:
        errors.append("Marque pelo menos uma loja (Mercado Livre, Shopee ou Amazon).")
    bad_nodes = [c for c in offer_config.split_list(amazon_nodes) if not c.isdigit()]
    if bad_nodes:
        errors.append(f"Categoria da Amazon inválida: {', '.join(bad_nodes)} (use os números das categorias).")
    bad_cats = [c for c in offer_config.split_list(shopee_categories) if not c.isdigit()]
    if bad_cats:
        errors.append(f"Categoria da Shopee inválida: {', '.join(bad_cats)} (use os números das categorias).")
    if active_hours.strip() and offer_config.parse_hours(active_hours) == (None, None):
        errors.append("Horário ativo inválido: use o formato 08:00-23:00 (ou deixe vazio para 24h).")
    all_categories = [c for c in categories if c in offer_config.ML_CATEGORIES]
    for c in offer_config.split_list(extra_categories):
        if not re.fullmatch(r"MLB\d+", c.upper()):
            errors.append(f"Categoria inválida: {c} (formato MLB1234).")
        else:
            all_categories.append(c.upper())
    if errors:
        for e in errors:
            flash(request, e, "error")
        return redirect(_back(bot_id, "busca"))
    bot_service.update_config(bot, {
        "mode": "manual" if mode == "manual" else "auto",
        "interval_minutes": interval_minutes, "max_posts_per_cycle": max_posts_per_cycle,
        "spacing_seconds": spacing_seconds, "min_discount": min_discount, "min_price": min_price,
        "max_price": max_price, "blocked_words": blocked_words.strip(),
        "categories": ",".join(dict.fromkeys(all_categories)), "keywords": keywords.strip(),
        "no_repeat_days": no_repeat_days, "active_hours": active_hours.strip(),
        "src_mercadolivre": "1" if src_mercadolivre else "0", "src_shopee": "1" if src_shopee else "0",
        "shopee_keywords": shopee_keywords.strip(), "shopee_categories": shopee_categories.strip(),
        "shopee_sort": shopee_sort,
        "src_amazon": "1" if src_amazon else "0", "amazon_keywords": amazon_keywords.strip(),
        "amazon_nodes": amazon_nodes.strip(), "amazon_sort": amazon_sort,
        "skip_fake": "1" if skip_fake else "0",
    })
    audit(db, request, admin, "offer_bot_search_saved", bot_id=bot.id, categories=len(all_categories))
    db.commit()
    flash(request, "Busca e filtros salvos. Valem a partir do próximo ciclo (sem reiniciar).")
    return redirect(_back(bot_id, "busca"))


@router.post("/offers/bots/{bot_id}/template")
def bot_template(
    bot_id: int,
    request: Request,
    template: str = Form("", max_length=1500),
    default_headline: str = Form("", max_length=120),
    footer: str = Form("", max_length=500),
    reset_template: str = Form(""),
    _csrf: None = Depends(verify_csrf),
    admin: AdminUser = Depends(require_superuser),
    db: Session = Depends(get_db),
):
    bot = _bot(db, bot_id, lock=True)
    text = "" if reset_template else template.replace("\r\n", "\n").strip()
    errors = post_template.validate(text) if text else []
    if errors:
        for e in errors:
            flash(request, e, "error")
        return redirect(_back(bot_id, "post"))
    bot_service.update_config(bot, {
        "template": "" if text == post_template.DEFAULT_TEMPLATE else text,
        "default_headline": "" if reset_template else default_headline.strip(), "footer": footer.strip(),
    })
    audit(db, request, admin, "offer_bot_template_saved", bot_id=bot.id)
    db.commit()
    flash(request, "Modelo do post salvo.")
    return redirect(_back(bot_id, "post"))


def _test_stores(cfg, ml: bool, shop: bool, amz: bool = False) -> list[dict]:
    steps: list[tuple[str, bool, str]] = []
    if ml:
        try:
            with ml_client_factory(cfg) as client:
                steps += [(f"Mercado Livre · {s}", ok, d) for s, ok, d in
                          client.test_connection((cfg.categories or ["MLB1051"])[0])]
        except MLError as exc:
            steps.append(("Mercado Livre · Autenticação", False, str(exc)))
    if shop:
        try:
            with shopee_client_factory(cfg) as client:
                steps += [(f"Shopee · {s}", ok, d) for s, ok, d in client.test_connection()]
        except ShopeeError as exc:
            steps.append(("Shopee · Autenticação", False, str(exc)))
    if amz:
        try:
            with amazon_client_factory(cfg) as client:
                steps += [(f"Amazon · {s}", ok, d) for s, ok, d in client.test_connection()]
        except AmazonError as exc:
            steps.append(("Amazon · Autenticação", False, str(exc)))
    if not steps:
        steps.append(("Lojas", False, "nenhuma loja ligada/configurada"))
    return [{"step": s, "ok": ok, "detail": d[:300]} for s, ok, d in steps]


@router.post("/offers/bots/{bot_id}/test")
def bot_test(bot_id: int, request: Request, _csrf: None = Depends(verify_csrf),
             admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    cfg = bot_service.config_for(db, _bot(db, bot_id))
    request.session["offers_test"] = _test_stores(cfg, cfg.use_mercadolivre, cfg.use_shopee, cfg.use_amazon)
    return redirect(_back(bot_id))


@router.post("/offers/bots/{bot_id}/run")
def bot_run(bot_id: int, request: Request, _csrf: None = Depends(verify_csrf),
            admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    _bot(db, bot_id)
    result = collector.run_cycle(bot_id, ml_client_factory, shopee_factory=shopee_client_factory,
                                 amazon_factory=amazon_client_factory,
                                 triggered_by=f"painel:{admin.username}")
    kind = "error" if result.errors and not result.eligible else ("warn" if result.errors else "ok")
    flash(request, "Busca concluída: " + result.summary(), kind)
    return redirect(f"/admin/offers?bot={bot_id}")


# --------------------------------------------------------------------------- #
# WhatsApp do bot (número próprio)
# --------------------------------------------------------------------------- #
def _whatsapp_context(bot: OfferBot, cfg, admin: AdminUser, want_qr: bool) -> dict:
    wa = whatsapp.wa_config(bot, cfg)
    state, number, qr_image, groups, error = "unknown", None, None, [], None
    try:
        with wa_client_factory() as client:
            state = client.state(wa.instance)
            if state == "open":
                number = client.owner_number(wa.instance)
                try:
                    groups = client.groups(wa.instance)
                except WhatsAppError as exc:
                    error = f"Não foi possível listar os grupos: {exc}"
            elif want_qr and state in ("connecting", "close") and admin.is_superuser:
                qr_image = client.qr_code(wa.instance)
    except WhatsAppError as exc:
        error = str(exc)
    return {"wa": wa, "wa_state": state, "wa_number": number, "qr_image": qr_image, "wa_groups": groups,
            "wa_selected": {g["id"] for g in wa.groups}, "wa_error": error,
            "limits": whatsapp.limits_status(bot.id, wa, cfg), "waiting_qr": want_qr and state != "open"}


@router.post("/offers/bots/{bot_id}/whatsapp/connect")
def wa_connect(bot_id: int, request: Request, _csrf: None = Depends(verify_csrf),
               admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    bot = _bot(db, bot_id)
    try:
        with wa_client_factory() as client:
            if client.state(bot.wa_instance) == "missing":
                client.create(bot.wa_instance)
    except WhatsAppError as exc:
        flash(request, str(exc), "error")
        return redirect(_back(bot_id, "whatsapp"))
    audit(db, request, admin, "whatsapp_connect_started", bot_id=bot.id)
    db.commit()
    return redirect(_back(bot_id, "whatsapp") + "&qr=1")


@router.post("/offers/bots/{bot_id}/whatsapp/logout")
def wa_logout(bot_id: int, request: Request, _csrf: None = Depends(verify_csrf),
              admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    bot = _bot(db, bot_id, lock=True)
    try:
        with wa_client_factory() as client:
            client.logout(bot.wa_instance)
        bot_service.update_config(bot, {"wa_enabled": "0"})
        audit(db, request, admin, "whatsapp_logout", bot_id=bot.id)
        db.commit()
        flash(request, "Número desconectado e envios pausados.")
    except WhatsAppError as exc:
        flash(request, str(exc), "error")
    return redirect(_back(bot_id, "whatsapp"))


@router.post("/offers/bots/{bot_id}/whatsapp/groups")
def wa_groups(bot_id: int, request: Request, group_ids: list[str] = Form(default=[]),
              _csrf: None = Depends(verify_csrf), admin: AdminUser = Depends(require_superuser),
              db: Session = Depends(get_db)):
    bot = _bot(db, bot_id, lock=True)
    ids = [g for g in dict.fromkeys(group_ids) if bot_service.WA_GROUP_RE.match(g)][:20]
    try:
        with wa_client_factory() as client:
            names = {g["id"]: g["name"] for g in client.groups(bot.wa_instance)}
    except WhatsAppError:
        names = {t.target_id: t.name for t in bot.targets if t.channel == TargetChannel.WHATSAPP}
    count = bot_service.set_whatsapp_groups(db, bot, [{"id": i, "name": names[i]} for i in ids if i in names])
    audit(db, request, admin, "whatsapp_groups_saved", bot_id=bot.id, groups=count)
    db.commit()
    flash(request, f"{count} grupo(s) selecionado(s) para receber as ofertas deste bot.")
    return redirect(_back(bot_id, "whatsapp"))


@router.post("/offers/bots/{bot_id}/whatsapp/settings")
def wa_settings(
    bot_id: int,
    request: Request,
    wa_warmup_days: str = Form("7", max_length=3),
    wa_warmup_per_day: str = Form("5", max_length=3),
    wa_max_per_window: str = Form("5", max_length=3),
    wa_window_hours: str = Form("4", max_length=3),
    wa_max_per_day: str = Form("20", max_length=3),
    wa_group_delay_seconds: str = Form("10", max_length=4),
    wa_max_age_hours: str = Form("6", max_length=3),
    _csrf: None = Depends(verify_csrf),
    admin: AdminUser = Depends(require_superuser),
    db: Session = Depends(get_db),
):
    bot = _bot(db, bot_id, lock=True)
    raw = {"wa_warmup_days": wa_warmup_days, "wa_warmup_per_day": wa_warmup_per_day,
           "wa_max_per_window": wa_max_per_window, "wa_window_hours": wa_window_hours,
           "wa_max_per_day": wa_max_per_day, "wa_group_delay_seconds": wa_group_delay_seconds,
           "wa_max_age_hours": wa_max_age_hours}
    c = offer_config.parse(raw)  # aplica os limites mínimos/máximos de segurança
    bot_service.update_config(bot, {
        "wa_warmup_days": c.wa_warmup_days, "wa_warmup_per_day": c.wa_warmup_per_day,
        "wa_max_per_window": c.wa_max_per_window, "wa_window_hours": c.wa_window_hours,
        "wa_max_per_day": c.wa_max_per_day, "wa_group_delay_seconds": c.wa_group_delay_seconds,
        "wa_max_age_hours": c.wa_max_age_hours,
    })
    audit(db, request, admin, "whatsapp_limits_saved", bot_id=bot.id)
    db.commit()
    flash(request, "Ritmo de envio salvo.")
    return redirect(_back(bot_id, "whatsapp"))


@router.post("/offers/bots/{bot_id}/whatsapp/warmup/reset")
def wa_warmup_reset(bot_id: int, request: Request, _csrf: None = Depends(verify_csrf),
                    admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    _bot(db, bot_id)
    bot_service.update_state(db, bot_id, wa_started_at=utcnow().isoformat())
    audit(db, request, admin, "whatsapp_warmup_reset", bot_id=bot_id)
    db.commit()
    flash(request, "Aquecimento reiniciado a partir de hoje.")
    return redirect(_back(bot_id, "whatsapp"))


@router.post("/offers/bots/{bot_id}/whatsapp/toggle")
def wa_toggle(bot_id: int, request: Request, _csrf: None = Depends(verify_csrf),
              admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    bot = _bot(db, bot_id, lock=True)
    cfg = bot_service.config_for(db, bot)
    enable = not cfg.wa_enabled
    if enable and not bot_service.active_targets(bot, TargetChannel.WHATSAPP):
        flash(request, "Selecione pelo menos um grupo antes de ativar os envios.", "error")
        return redirect(_back(bot_id, "whatsapp"))
    bot_service.update_config(bot, {"wa_enabled": "1" if enable else "0"})
    if enable and not (bot.state or {}).get("wa_started_at"):
        state = dict(bot.state or {})
        state["wa_started_at"] = utcnow().isoformat()  # o aquecimento começa na 1ª ativação
        bot.state = state
    audit(db, request, admin, "whatsapp_toggled", bot_id=bot.id, enabled=enable)
    db.commit()
    msg = "Envios para o WhatsApp ativados." if enable else "Envios para o WhatsApp pausados."
    if enable and not bot.is_active:
        msg += " Lembre de LIGAR o bot (aba Geral) para os envios começarem."
    flash(request, msg)
    return redirect(_back(bot_id, "whatsapp"))


@router.post("/offers/bots/{bot_id}/whatsapp/test")
def wa_test(bot_id: int, request: Request, group_id: str = Form(...), _csrf: None = Depends(verify_csrf),
            admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    bot = _bot(db, bot_id)
    group = next((t for t in bot_service.active_targets(bot, TargetChannel.WHATSAPP) if t.target_id == group_id), None)
    if group is None:
        flash(request, "Escolha um dos grupos selecionados.", "error")
        return redirect(_back(bot_id, "whatsapp"))
    try:
        with wa_client_factory() as client:
            client.send_text(bot.wa_instance, group.target_id, "Teste de conexão do bot de ofertas: tudo certo por aqui.")
        flash(request, f"Mensagem de teste enviada para “{group.name}”.")
    except WhatsAppError as exc:
        flash(request, f"Falha no envio: {exc}", "error")
    return redirect(_back(bot_id, "whatsapp"))


# --------------------------------------------------------------------------- #
# Ofertas de cada bot
# --------------------------------------------------------------------------- #
@router.get("/offers", response_class=HTMLResponse)
def offers_list(request: Request, bot: int | None = None, status: str = "new", page: int = 1,
                admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    bots = db.scalars(select(OfferBot).order_by(OfferBot.id)).all()
    if not bots:
        flash(request, "Crie um bot de ofertas primeiro.", "warn")
        return redirect("/admin/offers/bots")
    current = next((b for b in bots if b.id == bot), bots[0])
    stmt = (select(OfferPost).options(selectinload(OfferPost.offer))
            .join(PromoOffer, PromoOffer.id == OfferPost.offer_id).where(OfferPost.bot_id == current.id))
    if status in {s.value for s in OfferStatus}:
        stmt = stmt.where(OfferPost.status == OfferStatus(status))
    else:
        status = "all"
    if status == "posted":
        stmt = stmt.order_by(OfferPost.posted_at.desc().nulls_last())
    elif status == "queued":
        stmt = stmt.order_by(OfferPost.queued_at)
    else:
        stmt = stmt.order_by(PromoOffer.discount_pct.desc().nulls_last(), OfferPost.id.desc())
    posts, pager = paginate(db, stmt, page)
    cfg = bot_service.config_for(db, current)
    previews = {p.id: telegram_caption(p.offer, cfg) for p in posts}
    return render(request, "admin/offers_list.html", admin, bots=bots, current=current, posts=posts, pager=pager,
                  status=status, statuses=[s.value for s in OfferStatus], stats=collector.stats(current.id),
                  previews=previews, wa_ready=whatsapp.wa_config(current, cfg).ready,
                  has_tg=bool(bot_service.active_targets(current, TargetChannel.TELEGRAM)))


def _offer_back(request: Request, bot_id: int) -> str:
    return request.headers.get("referer") or f"/admin/offers?bot={bot_id}"


def _save_details(db: Session, offer_id: int, affiliate_url: str, headline: str | None, coupon: str | None,
                  request: Request) -> bool:
    url = valid_affiliate(affiliate_url)
    if url is None:
        flash(request, "Link de afiliado inválido (precisa começar com https://).", "error")
        return False
    offer = db.get(PromoOffer, offer_id, with_for_update=True)
    if offer is None:
        raise HTTPException(404)
    collector.set_offer_details(db, offer, url, headline, coupon)
    db.commit()
    return True


@router.post("/offers/bots/{bot_id}/offers/{offer_id}/save")
def offer_save(bot_id: int, offer_id: int, request: Request, affiliate_url: str = Form("", max_length=500),
               headline: str | None = Form(None, max_length=120), coupon: str | None = Form(None, max_length=40),
               _csrf: None = Depends(verify_csrf), admin: AdminUser = Depends(current_admin),
               db: Session = Depends(get_db)):
    if _save_details(db, offer_id, affiliate_url, headline, coupon, request):
        flash(request, "Oferta salva.")
    return redirect(_offer_back(request, bot_id))


@router.post("/offers/bots/{bot_id}/offers/{offer_id}/queue")
def offer_queue(bot_id: int, offer_id: int, request: Request, target: str = Form("both"),
                affiliate_url: str = Form("", max_length=500), headline: str | None = Form(None, max_length=120),
                coupon: str | None = Form(None, max_length=40), _csrf: None = Depends(verify_csrf),
                admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    _bot(db, bot_id)
    if not _save_details(db, offer_id, affiliate_url, headline, coupon, request):
        return redirect(_offer_back(request, bot_id))
    ok = collector.queue_post(offer_id, bot_id, telegram=target in ("both", "telegram"),
                              whatsapp_too=target in ("both", "whatsapp"), admin_id=admin.id, ip=client_ip(request))
    if ok:
        flash(request, "Oferta na fila — sai respeitando o intervalo entre posts e o ritmo do WhatsApp.")
    else:
        flash(request, "Nada a fazer: a oferta já está na fila ou o bot não tem destinos ativos.", "warn")
    return redirect(_offer_back(request, bot_id))


@router.post("/offers/bots/{bot_id}/import")
def offer_import(bot_id: int, request: Request, link: str = Form("", max_length=500),
                 headline: str = Form("", max_length=120), coupon: str = Form("", max_length=40),
                 target: str = Form("prepare"), _csrf: None = Depends(verify_csrf),
                 admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    """Post manual: cola o link (Shopee, Mercado Livre ou Amazon), a API traz foto, preço e título."""
    _bot(db, bot_id)
    db.rollback()   # libera a sessão: import_link abre a sua própria
    text = link.strip()
    if headline.strip():
        text += f"\nchamada: {headline.strip()}"
    if coupon.strip():
        text += f"\ncupom: {coupon.strip()}"
    try:
        preview = converter.import_link(bot_id, text, client_factory=ml_client_factory,
                                        shopee_factory=shopee_client_factory, amazon_factory=amazon_client_factory)
    except converter.ConvertError as exc:
        flash(request, str(exc), "error")
        return redirect(f"/admin/offers?bot={bot_id}")
    if target not in ("both", "telegram", "whatsapp"):
        flash(request, "Produto importado. Confira a prévia e clique em Publicar.")
        return redirect(f"/admin/offers?bot={bot_id}&status=new")
    ok = collector.queue_post(preview.offer_id, bot_id, telegram=target in ("both", "telegram"),
                              whatsapp_too=target in ("both", "whatsapp"), admin_id=admin.id, ip=client_ip(request),
                              first=True)
    if ok:
        flash(request, "Produto importado e na fila — é o próximo a sair, respeitando o intervalo entre posts.")
        return redirect(f"/admin/offers?bot={bot_id}&status=queued")
    flash(request, "Produto importado, mas o bot não tem destinos ativos (ou já estava na fila).", "warn")
    return redirect(f"/admin/offers?bot={bot_id}&status=all")


@router.post("/offers/bots/{bot_id}/offers/{offer_id}/discard")
def offer_discard(bot_id: int, offer_id: int, request: Request, _csrf: None = Depends(verify_csrf),
                  admin: AdminUser = Depends(current_admin)):
    collector.discard_post(offer_id, bot_id, admin.id, client_ip(request))
    return redirect(_offer_back(request, bot_id))


@router.post("/offers/bots/{bot_id}/offers/{offer_id}/restore")
def offer_restore(bot_id: int, offer_id: int, request: Request, _csrf: None = Depends(verify_csrf),
                  admin: AdminUser = Depends(current_admin)):
    collector.restore_post(offer_id, bot_id)
    return redirect(_offer_back(request, bot_id))


@router.get("/offers/stats", response_class=HTMLResponse)
def offers_stats(request: Request, bot: int | None = None, days: int = 30,
                 admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    bots = db.scalars(select(OfferBot).order_by(OfferBot.id)).all()
    current = next((b for b in bots if b.id == bot), None)
    data = offer_stats.build(db, current.id if current else None, days, utcnow())
    return render(request, "admin/offers_stats.html", admin, bots=bots, current=current, s=data,
                  periods=offer_stats.PERIODS, store_names=dict(offer_stats.STORES))


# --------------------------------------------------------------------------- #
# Configurações do módulo: contas das lojas (Mercado Livre, Shopee e Amazon)
# --------------------------------------------------------------------------- #
@router.get("/offers/settings", response_class=HTMLResponse)
def offers_settings(request: Request, admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    values = settings_service.get_all(db)
    return render(request, "admin/offers_settings.html", admin, values=values,
                  secret_mask=settings_service.mask(values.get("ml_secret_key", "")),
                  shopee_mask=settings_service.mask(values.get("shopee_secret", "")),
                  amazon_mask=settings_service.mask(values.get("amazon_secret", "")),
                  amazon_versions=amazon.VERSIONS,
                  test_results=request.session.pop("offers_test", None),
                  worker_online=alert_service.bot_online(values, key=OFFERS_HEARTBEAT),
                  sales_bot=settings.telegram_bot_username)


@router.post("/offers/settings")
def offers_settings_save(request: Request, store: str = Form("mercadolivre"),
                         ml_app_id: str = Form("", max_length=64), ml_secret_key: str = Form("", max_length=128),
                         shopee_app_id: str = Form("", max_length=64), shopee_secret: str = Form("", max_length=128),
                         amazon_credential_id: str = Form("", max_length=128),
                         amazon_secret: str = Form("", max_length=256),
                         amazon_partner_tag: str = Form("", max_length=64),
                         amazon_version: str = Form("3.1", max_length=8),
                         _csrf: None = Depends(verify_csrf), admin: AdminUser = Depends(require_superuser),
                         db: Session = Depends(get_db)):
    if store == "amazon":
        tag = amazon_partner_tag.strip()
        if tag and not amazon.valid_tag(tag):
            flash(request, "Partner Tag inválido: use o seu ID de associado, no formato meusite-20.", "error")
            return redirect("/admin/offers/settings")
        values = {"amazon_credential_id": amazon_credential_id.strip(), "amazon_partner_tag": tag,
                  "amazon_version": amazon_version if amazon_version in amazon.VERSIONS else "3.1"}
        if amazon_secret.strip():  # vazio = mantém o secret atual
            values["amazon_secret"] = amazon_secret.strip()
        settings_service.set_values(db, values, admin.id)
        audit(db, request, admin, "store_credentials_saved", store=store, secret_changed=bool(amazon_secret.strip()))
        db.commit()
        flash(request, "Credenciais da Amazon salvas. Valem para todos os bots de ofertas.")
        return redirect("/admin/offers/settings")
    if store == "shopee":
        app_id, secret, label = shopee_app_id.strip(), shopee_secret.strip(), "da Shopee"
        keys = ("shopee_app_id", "shopee_secret")
    else:
        app_id, secret, label = ml_app_id.strip(), ml_secret_key.strip(), "do Mercado Livre"
        keys = ("ml_app_id", "ml_secret_key")
    if app_id and not app_id.isdigit():
        flash(request, f"O App ID {label} é numérico.", "error")
        return redirect("/admin/offers/settings")
    values = {keys[0]: app_id}
    if secret:  # vazio = mantém a chave atual
        values[keys[1]] = secret
    settings_service.set_values(db, values, admin.id)
    audit(db, request, admin, "store_credentials_saved", store=store, secret_changed=bool(secret))
    db.commit()
    flash(request, f"Credenciais {label} salvas. Valem para todos os bots de ofertas.")
    return redirect("/admin/offers/settings")


@router.post("/offers/settings/test")
def offers_settings_test(request: Request, store: str = Form("mercadolivre"), _csrf: None = Depends(verify_csrf),
                         admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    values = settings_service.get_all(db)
    cfg = offer_config.parse({}, **bot_service.store_kwargs(values))
    request.session["offers_test"] = _test_stores(cfg, store == "mercadolivre", store == "shopee", store == "amazon")
    return redirect("/admin/offers/settings")
