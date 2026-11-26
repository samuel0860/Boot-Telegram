"""Painel administrativo (HTML renderizado no servidor).

Permissões:
- Qualquer administrador ativo: consultar tudo, bloquear/desbloquear usuários
  e encerrar acessos.
- Superusuário: gerenciar planos, preços, categorias, conteúdos, cupons, bots de ofertas e administradores.

As telas ficam em admin/views/ (um arquivo por assunto).
"""
from __future__ import annotations

from fastapi import APIRouter

from admin.views import (
    accounts,
    auth_views,
    catalog,
    coupons,
    dashboard,
    funnel,
    history,
    offers,
    platform,
    pwa,
    sales,
)

router = APIRouter(prefix="/admin", include_in_schema=False)
for _module in (pwa, auth_views, dashboard, sales, funnel, coupons, catalog, history, accounts, offers, platform):
    router.include_router(_module.router)
