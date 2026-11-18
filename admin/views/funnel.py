"""Funil de vendas por origem (o parâmetro do link t.me/SeuBot?start=ORIGEM)."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from admin.auth import current_admin
from admin.common import render
from config import settings
from core.audit import utcnow
from database import get_db
from models import AdminUser
from sales import sales_service

router = APIRouter()
PERIODS = {"7": "7 dias", "30": "30 dias", "90": "90 dias", "all": "Tudo"}


@router.get("/funnel", response_class=HTMLResponse)
def funnel(request: Request, period: str = "30", admin: AdminUser = Depends(current_admin),
           db: Session = Depends(get_db)):
    if period not in PERIODS:
        period = "30"
    since = None if period == "all" else utcnow() - timedelta(days=int(period))
    rows = sales_service.funnel_by_source(db, since)
    total = sales_service.FunnelRow(
        source="Total", users=sum(r.users for r in rows), checkout_users=sum(r.checkout_users for r in rows),
        buyers=sum(r.buyers for r in rows), orders_paid=sum(r.orders_paid for r in rows),
        revenue=sum((r.revenue for r in rows), Decimal("0")),
    )
    return render(request, "admin/funnel.html", admin, rows=rows, total=total, period=period, periods=PERIODS,
                  bot_username=settings.telegram_bot_username)
