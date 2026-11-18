"""Avisa os administradores no Telegram quando acontece um ERRO no sistema.

Um handler de logging guarda em memória os registros de nível ERROR (inclusive
os `logger.exception(...)` dos jobs). De tempos em tempos eles são enviados,
agrupados, para os administradores vinculados — sem repetir o mesmo erro por
ERROR_COOLDOWN e com um teto de mensagens por envio.

- Bots (vendas e ofertas): o job "errors" chama `flush_async(bot)`.
- Backend: `start_backend_thread()` envia pela API HTTP do Telegram.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from html import escape

from config import settings

ERROR_COOLDOWN = 30 * 60      # segundos sem repetir o mesmo erro
MAX_PER_FLUSH = 5             # mensagens por envio (evita inundar o Telegram)
_IGNORED_LOGGERS = ("core.error_reporter", "telegram.ext.Updater", "httpx", "httpcore")

logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    key: str
    process: str
    where: str
    message: str
    detail: str
    count: int = 1


class ErrorBuffer(logging.Handler):
    def __init__(self, process: str, maxlen: int = 200):
        super().__init__(level=logging.ERROR)
        self.process = process
        self._items: deque[_Entry] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._sent: dict[str, float] = {}

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(_IGNORED_LOGGERS):
            return
        try:
            message = record.getMessage()
            detail = ""
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                detail = f"{exc.__class__.__name__}: {exc}"
                last = traceback.extract_tb(record.exc_info[2])[-1:] if record.exc_info[2] else []
                if last:
                    detail += f"\n↳ {last[0].filename.rsplit('/', 1)[-1]}:{last[0].lineno} ({last[0].name})"
            where = f"{record.name}"
            key = hashlib.blake2b(f"{where}|{message[:120]}|{detail.split(':', 1)[0]}".encode(), digest_size=12).hexdigest()
            with self._lock:
                for item in self._items:
                    if item.key == key:
                        item.count += 1
                        return
                self._items.append(_Entry(key, self.process, where, _redact(message), _redact(detail)))
        except Exception:  # noqa: BLE001, S110 — um handler de log nunca pode derrubar o sistema
            pass

    def drain(self) -> list[str]:
        """Mensagens prontas para enviar (respeita cooldown e teto)."""
        now = time.monotonic()
        with self._lock:
            items = list(self._items)
            self._items.clear()
        self._sent = {k: t for k, t in self._sent.items() if now - t < ERROR_COOLDOWN}
        out = []
        skipped = 0
        for item in items:
            if item.key in self._sent:
                continue
            if len(out) >= MAX_PER_FLUSH:
                skipped += 1
                continue
            self._sent[item.key] = now
            out.append(_format(item))
        if skipped:
            out.append(f"➕ Mais {skipped} erro(s) diferente(s). Veja os logs: <code>make logs</code>")
        return out


def _redact(text: str) -> str:
    for secret in (settings.telegram_bot_token, settings.mp_access_token, settings.evolution_api_key):
        if secret and secret in text:
            text = text.replace(secret, "***")
    return text


def _format(item: _Entry) -> str:
    times = f" (×{item.count})" if item.count > 1 else ""
    text = (f"🛑 <b>Erro no {escape(item.process)}</b>{times}\n"
            f"<code>{escape(item.where)}</code>\n{escape(item.message[:400])}")
    if item.detail:
        text += f"\n<pre>{escape(item.detail[:600])}</pre>"
    return text


_buffer: ErrorBuffer | None = None


def install(process: str) -> ErrorBuffer:
    """Liga o captador de erros no logger raiz (uma vez por processo)."""
    global _buffer
    if _buffer is None:
        _buffer = ErrorBuffer(process)
        logging.getLogger().addHandler(_buffer)
    return _buffer


def _admin_ids() -> set[int]:
    from core import alert_service, settings_service
    from database import session_scope

    with session_scope() as session:
        values = settings_service.get_all(session)
    if values.get("alerts_enabled") != "1":
        return set()
    return alert_service.admin_ids(values)


async def flush_async(bot) -> int:
    """Usado pelo bot. Retorna quantas mensagens foram enviadas."""
    import asyncio

    from telegram.constants import ParseMode
    from telegram.error import TelegramError

    async def safe_send(chat_id: int, text: str) -> None:
        try:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except TelegramError:
            pass  # admin que nunca abriu este bot, por exemplo

    if _buffer is None:
        return 0
    messages = _buffer.drain()
    if not messages:
        return 0
    try:
        admins = await asyncio.to_thread(_admin_ids)
    except Exception:  # noqa: BLE001
        return 0
    for admin_id in admins:
        for text in messages:
            await safe_send(admin_id, text)
    return len(messages)


def flush_http(client=None) -> int:
    """Usado pelo backend (não tem o objeto Bot): chama a API HTTP do Telegram."""
    import httpx

    if _buffer is None or not settings.telegram_bot_token:
        return 0
    messages = _buffer.drain()
    if not messages:
        return 0
    try:
        admins = _admin_ids()
    except Exception:  # noqa: BLE001
        return 0
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    own = client is None
    client = client or httpx.Client(timeout=10)
    try:
        for admin_id in admins:
            for text in messages:
                try:
                    client.post(url, json={"chat_id": admin_id, "text": text, "parse_mode": "HTML",
                                           "disable_web_page_preview": True})
                except httpx.HTTPError:
                    pass
    finally:
        if own:
            client.close()
    return len(messages)


def start_backend_thread(interval: float = 60.0) -> None:
    install("painel/backend")

    def _loop():
        while True:
            time.sleep(interval)
            try:
                flush_http()
            except Exception:  # noqa: BLE001, S110 — o aviso de erro não pode gerar novos erros
                pass

    threading.Thread(target=_loop, name="error-reporter", daemon=True).start()
