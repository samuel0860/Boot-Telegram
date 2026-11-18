"""MÓDULO DE VENDAS — bot próprio do Telegram (planos, PIX, entrega, assinaturas).

Execução:  python sales_bot.py

Não tem nada de ofertas/afiliados: isso é o módulo de ofertas (offers_bot.py).

Os handlers ficam no pacote telegram_bot/:
  common.py  teclados e utilidades     sales.py   menu, planos, cupons e PIX
  router.py  botões (callback_data)    admin.py   ferramentas dos administradores
  groups.py  entrada em grupos         jobs.py    rotinas periódicas
"""
from __future__ import annotations

import logging
import time

from telegram import BotCommand, Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import ConfigError, configure_logging, settings
from core import error_reporter
from database import check_connection
from sales.bot import admin, groups, jobs, sales
from sales.bot.router import on_callback

logger = logging.getLogger("bot")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Erro ao processar update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        try:
            await update.effective_chat.send_message("⚠️ Ocorreu um erro inesperado. Tente novamente em instantes.")
        except TelegramError:
            pass


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Menu principal"),
            BotCommand("planos", "Ver planos e preços"),
            BotCommand("cupom", "Usar cupom de desconto"),
            BotCommand("meusacessos", "Meus acessos"),
            BotCommand("suporte", "Falar com o suporte"),
        ]
    )
    me = await app.bot.get_me()
    logger.info("Bot iniciado como @%s", me.username)
    if settings.telegram_bot_username and me.username.lower() != settings.telegram_bot_username.lower():
        logger.warning("TELEGRAM_BOT_USERNAME (%s) difere do bot real (@%s) — o link da página ficará errado",
                       settings.telegram_bot_username, me.username)


def build_application() -> Application:
    app = ApplicationBuilder().token(settings.telegram_bot_token).post_init(post_init).build()

    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", sales.start, filters=private))
    app.add_handler(CommandHandler(["help", "ajuda"], sales.help_cmd, filters=private))
    app.add_handler(CommandHandler("planos", sales.cmd_plans, filters=private))
    app.add_handler(CommandHandler("meusacessos", sales.cmd_access, filters=private))
    app.add_handler(CommandHandler("suporte", sales.cmd_support, filters=private))
    app.add_handler(CommandHandler("cupom", sales.cmd_coupon, filters=private))
    app.add_handler(CommandHandler("chatid", admin.admin_chat_id, filters=filters.ChatType.GROUPS | private))
    app.add_handler(CommandHandler("id", admin.cmd_id, filters=private))
    app.add_handler(CommandHandler("vincular", admin.cmd_vincular, filters=private))
    app.add_handler(CommandHandler("resumo", admin.cmd_resumo, filters=private))
    app.add_handler(CommandHandler("status", admin.cmd_status, filters=private))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, admin.channel_post_chat_id))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(ChatMemberHandler(groups.on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    # Ferramentas de admin (a permissão é verificada dentro de cada handler)
    app.add_handler(MessageHandler(private & filters.FORWARDED, admin.admin_forwarded))
    app.add_handler(MessageHandler(private & (filters.Document.ALL | filters.PHOTO | filters.VIDEO), admin.admin_file_id))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, sales.fallback_text))
    app.add_error_handler(on_error)

    jq = app.job_queue
    jq.run_repeating(jobs.job_deliveries, interval=10, first=5, name="deliveries")
    jq.run_repeating(jobs.job_reconcile, interval=120, first=30, name="reconcile")
    jq.run_repeating(jobs.job_expirations, interval=300, first=20, name="expirations")
    jq.run_repeating(jobs.job_recoveries, interval=120, first=40, name="pix_recovery")
    jq.run_repeating(jobs.job_retention, interval=6 * 3600, first=300, name="retention")
    jq.run_repeating(jobs.job_health, interval=60, first=10, name="health")
    if settings.error_reports:
        jq.run_repeating(jobs.job_errors, interval=60, first=60, name="errors")
    return app


def main() -> None:
    configure_logging()
    if settings.error_reports:
        error_reporter.install("bot de vendas")
    try:
        settings.require_bot()
    except ConfigError as exc:
        logger.error("Configuração inválida: %s — ajuste o .env e reinicie o serviço.", exc)
        time.sleep(30)  # evita reinícios em loop rápido no Docker
        raise SystemExit(1) from None
    if not check_connection():
        raise SystemExit("Não foi possível conectar ao PostgreSQL. Verifique DATABASE_URL.")
    if not settings.payments_configured:
        logger.warning("MP_ACCESS_TOKEN não configurado — compras ficarão indisponíveis.")
    app = build_application()
    # chat_member precisa ser solicitado explicitamente
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
