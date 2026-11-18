"""Comandos administrativos.

    python manage.py create-admin --username admin [--email x@y] [--superuser]
    python manage.py set-password --username admin
    python manage.py seed [--file seed_data.json]
    python manage.py check
    python manage.py reconcile
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from decimal import Decimal

from sqlalchemy import func, select, text

from config import BASE_DIR, configure_logging, settings


def _read_password(confirm: bool = True) -> str:
    env_password = os.getenv("ADMIN_PASSWORD")
    if env_password:
        return env_password
    password = getpass.getpass("Senha: ")
    if confirm and getpass.getpass("Confirme a senha: ") != password:
        sys.exit("As senhas não conferem.")
    return password


def cmd_create_admin(args) -> None:
    from admin.auth import hash_password
    from database import session_scope
    from models import AdminUser

    username = args.username.strip().lower()
    password = _read_password()
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        sys.exit(str(exc))
    with session_scope() as session:
        if session.scalar(select(AdminUser).where(func.lower(AdminUser.username) == username)):
            sys.exit(f"Administrador '{username}' já existe. Use set-password para trocar a senha.")
        session.add(AdminUser(username=username, email=(args.email or None), password_hash=password_hash,
                              is_superuser=args.superuser))
    print(f"Administrador '{username}' criado{' (superusuário)' if args.superuser else ''}.")


def cmd_set_password(args) -> None:
    from admin.auth import hash_password
    from database import session_scope
    from models import AdminUser

    password = _read_password()
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        sys.exit(str(exc))
    with session_scope() as session:
        admin = session.scalar(select(AdminUser).where(func.lower(AdminUser.username) == args.username.lower()))
        if admin is None:
            sys.exit("Administrador não encontrado.")
        admin.password_hash = password_hash
        admin.failed_login_attempts = 0
        admin.locked_until = None
        admin.is_active = True
    print("Senha atualizada.")


def cmd_seed(args) -> None:
    from database import session_scope
    from models import AccessType, Category, ContentType, Plan, Product

    path = args.file or (BASE_DIR / "seed_data.json")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    created = {"categories": 0, "products": 0, "plans": 0}
    with session_scope() as session:
        cats = {}
        for c in data.get("categories", []):
            cat = session.scalar(select(Category).where(Category.slug == c["slug"]))
            if cat is None:
                cat = Category(slug=c["slug"], name=c["name"], description=c.get("description"),
                               sort_order=c.get("sort_order", 0))
                session.add(cat)
                created["categories"] += 1
            cats[c["slug"]] = cat
        session.flush()

        products = {}
        for p in data.get("products", []):
            prod = session.scalar(select(Product).where(Product.slug == p["slug"]))
            if prod is None:
                category = cats.get(p.get("category")) or session.scalar(
                    select(Category).where(Category.slug == p.get("category")))
                prod = Product(slug=p["slug"], name=p["name"], description=p.get("description"),
                               category=category, content_type=ContentType(p["content_type"]),
                               content_value=p["content_value"], sort_order=p.get("sort_order", 0),
                               is_active=p.get("is_active", True))
                session.add(prod)
                created["products"] += 1
            products[p["slug"]] = prod
        session.flush()

        for pl in data.get("plans", []):
            if session.scalar(select(Plan).where(Plan.slug == pl["slug"])):
                continue
            plan = Plan(slug=pl["slug"], name=pl["name"], description=pl.get("description"),
                        price=Decimal(pl["price"]), currency=settings.currency,
                        access_type=AccessType(pl["access_type"]), duration_days=pl.get("duration_days"),
                        sort_order=pl.get("sort_order", 0), is_active=pl.get("is_active", True))
            plan.products = [products[s] for s in pl.get("products", []) if s in products]
            session.add(plan)
            created["plans"] += 1
    print(f"Seed concluído: {created}")


def cmd_check(args) -> None:
    from database import engine

    ok = True
    print(f"Ambiente: {settings.app_env}")
    try:
        with engine.connect() as conn:
            version = conn.scalar(text("SHOW server_version"))
            rev = conn.scalar(text("SELECT version_num FROM alembic_version"))
        print(f"[ok] PostgreSQL {version} — migration atual: {rev}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"[ERRO] Banco de dados: {exc}")
    for label, value in [
        ("SECRET_KEY", len(settings.secret_key) >= 32),
        ("TELEGRAM_BOT_TOKEN", bool(settings.telegram_bot_token)),
        ("TELEGRAM_BOT_USERNAME", bool(settings.telegram_bot_username)),
        ("MP_ACCESS_TOKEN", bool(settings.mp_access_token)),
        ("MP_WEBHOOK_SECRET", bool(settings.mp_webhook_secret)),
        ("PUBLIC_BASE_URL (https)", settings.public_base_url.startswith("https://")),
    ]:
        print(f"[{'ok' if value else 'pendente'}] {label}")
    print(f"Link da página: {settings.telegram_link}")
    print(f"URL do webhook: {settings.webhook_url or '(defina PUBLIC_BASE_URL com https)'}")
    sys.exit(0 if ok else 1)


def cmd_reconcile(args) -> None:
    from sales.payment_service import reconcile_pending_orders
    from sales.payments import get_payment_client

    changed = reconcile_pending_orders(get_payment_client())
    print(f"Reconciliação concluída. Pedidos confirmados agora: {changed}")


def cmd_bot_health(args) -> None:
    """Healthcheck dos containers: falha se o sinal de vida do processo estiver parado."""
    from core import alert_service, settings_service
    from database import session_scope

    key = alert_service.OFFERS_HEARTBEAT if args.module == "offers" else alert_service.SALES_HEARTBEAT
    try:
        with session_scope() as session:
            values = settings_service.get_all(session)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"banco indisponível: {exc.__class__.__name__}")
    if not alert_service.bot_online(values, key=key):
        sys.exit(f"{args.module}: sem sinal de vida desde {values.get(key) or 'nunca'}")
    print("ok")


def cmd_purge(args) -> None:
    """LGPD: apaga agora os registros técnicos antigos (o bot também faz isso sozinho a cada 6 h)."""
    from core.retention_service import purge_old_records

    r = purge_old_records()
    print(f"Apagados: {r.logs} logs, {r.webhooks} webhooks, {r.payment_raw} respostas brutas de pagamento.")


def cmd_reset_2fa(args) -> None:
    """Para quem perdeu o celular: desliga a verificação em duas etapas da conta."""
    from core.audit import log_event
    from database import session_scope
    from models import AdminUser

    with session_scope() as session:
        admin = session.scalar(select(AdminUser).where(func.lower(AdminUser.username) == args.username.lower()))
        if admin is None:
            sys.exit("Administrador não encontrado.")
        admin.totp_enabled = False
        admin.totp_secret = None
        admin.totp_last_step = None
        log_event(session, "admin_2fa_reset_cli", admin_user_id=admin.id)
    print("Verificação em duas etapas desativada. Entre com a senha e ative de novo em Minha conta.")


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Gerenciamento do Telegram Vendas")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-admin", help="Cria um administrador do painel")
    p.add_argument("--username", required=True)
    p.add_argument("--email")
    p.add_argument("--superuser", action="store_true")
    p.set_defaults(func=cmd_create_admin)

    p = sub.add_parser("set-password", help="Redefine a senha de um administrador")
    p.add_argument("--username", required=True)
    p.set_defaults(func=cmd_set_password)

    p = sub.add_parser("bot-health", help="Healthcheck dos bots (usado pelo Docker)")
    p.add_argument("module", nargs="?", choices=["sales", "offers"], default="sales")
    p.set_defaults(func=cmd_bot_health)

    sub.add_parser("purge", help="LGPD: apaga registros técnicos antigos").set_defaults(func=cmd_purge)

    p = sub.add_parser("reset-2fa", help="Desativa a verificação em duas etapas de um administrador")
    p.add_argument("--username", required=True)
    p.set_defaults(func=cmd_reset_2fa)

    p = sub.add_parser("seed", help="Cria planos/categorias/conteúdos iniciais")
    p.add_argument("--file")
    p.set_defaults(func=cmd_seed)

    sub.add_parser("check", help="Verifica configuração e conexão").set_defaults(func=cmd_check)
    sub.add_parser("reconcile", help="Consulta pagamentos pendentes no provedor").set_defaults(func=cmd_reconcile)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
