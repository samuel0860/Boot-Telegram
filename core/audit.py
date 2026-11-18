"""Relógio (UTC) e registro de auditoria (access_logs), usados pelos dois módulos."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from models import AccessLog


def utcnow() -> datetime:
    return datetime.now(UTC)


def log_event(
    session: Session,
    action: str,
    *,
    user_id: int | None = None,
    admin_user_id: int | None = None,
    subscription_id: int | None = None,
    order_id: int | None = None,
    product_id: int | None = None,
    ip_address: str | None = None,
    **details: Any,
) -> None:
    session.add(
        AccessLog(
            action=action,
            user_id=user_id,
            admin_user_id=admin_user_id,
            subscription_id=subscription_id,
            order_id=order_id,
            product_id=product_id,
            ip_address=ip_address,
            details=details or None,
        )
    )
