"""Define a conta (cliente) da vez em cada requisição do painel.

Fica DENTRO do SessionMiddleware (lê a sessão já decodificada) e é ASGI puro: a conta vale
para toda a requisição, inclusive o código que roda em threads (o contexto é copiado).
"""
from __future__ import annotations

from core import tenancy


class AccountMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/admin"):
            await self.app(scope, receive, send)
            return
        session = scope.get("session") or {}
        account_id = session.get("account_id") if session.get("admin_id") else None
        token = tenancy.set_account(account_id if isinstance(account_id, int) else None)
        try:
            await self.app(scope, receive, send)
        finally:
            tenancy.reset(token)
