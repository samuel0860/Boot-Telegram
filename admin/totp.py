"""Verificação em duas etapas (TOTP, RFC 6238) — compatível com Google Authenticator,
Microsoft Authenticator, Authy, 1Password etc.

O segredo fica criptografado no banco (mesma chave das configurações do painel).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import secrets
import struct
import time
from urllib.parse import quote

import segno

from core.settings_service import decrypt, encrypt

PERIOD = 30
DIGITS = 6
ISSUER = "Telegram Vendas"


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((now if now is not None else time.time()) // PERIOD)


def verify(secret: str, code: str, *, last_step: int | None = None, now: float | None = None) -> int | None:
    """Retorna o passo de tempo aceito (para impedir reuso do mesmo código) ou None.

    Aceita 1 passo de tolerância (±30 s) para relógio de celular adiantado/atrasado.
    """
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != DIGITS or not secret:
        return None
    step = current_step(now)
    for candidate in (step - 1, step, step + 1):
        if last_step is not None and candidate <= last_step:
            continue
        if hmac.compare_digest(_code(secret, candidate), code):
            return candidate
    return None


def provisioning_uri(secret: str, username: str) -> str:
    label = quote(f"{ISSUER}:{username}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&digits={DIGITS}&period={PERIOD}"


def qr_svg(uri: str) -> str:
    """QR Code em SVG (inline, sem chamar serviços externos)."""
    buf = io.BytesIO()
    segno.make(uri, error="m").save(buf, kind="svg", scale=5, border=2, dark="#000", light="#fff",
                                    xmldecl=False, svgns=True, nl=False)
    return buf.getvalue().decode()


def format_secret(secret: str) -> str:
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


def store(secret: str) -> str:
    return encrypt(secret)


def load(stored: str | None) -> str:
    return decrypt(stored) if stored else ""
