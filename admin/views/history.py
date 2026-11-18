"""Histórico: logs de acesso e webhooks."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from admin.auth import (
    current_admin,
)
from admin.common import paginate, render
from database import get_db
from models import (
    AccessLog,
    AdminUser,
    WebhookEvent,
    WebhookStatus,
)

router = APIRouter()


# --------------------------------------------------------------------------- #
# Histórico: logs de acesso e webhooks
# --------------------------------------------------------------------------- #
@router.get("/logs", response_class=HTMLResponse)
def logs_list(request: Request, action: str = "", page: int = 1,
              admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    stmt = select(AccessLog).options(selectinload(AccessLog.user), selectinload(AccessLog.admin_user)) \
        .order_by(AccessLog.id.desc())
    if action:
        stmt = stmt.where(AccessLog.action == action)
    logs, pager = paginate(db, stmt, page)
    actions = db.scalars(select(AccessLog.action).distinct().order_by(AccessLog.action)).all()
    return render(request, "admin/logs.html", admin, logs=logs, pager=pager, action=action, actions=actions)


@router.get("/webhooks", response_class=HTMLResponse)
def webhooks_list(request: Request, status: str = "", page: int = 1,
                  admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    stmt = select(WebhookEvent).order_by(WebhookEvent.id.desc())
    if status in {s.value for s in WebhookStatus}:
        stmt = stmt.where(WebhookEvent.status == WebhookStatus(status))
    events, pager = paginate(db, stmt, page)
    return render(request, "admin/webhooks.html", admin, events=events, pager=pager, status=status,
                  statuses=[s.value for s in WebhookStatus])
