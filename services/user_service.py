"""Cadastro de usuários do Telegram."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from database import session_scope
from models import User
from services.access_service import log_event


@dataclass(frozen=True)
class UserInfo:
    id: int
    telegram_id: int
    first_name: str | None
    is_new: bool
    is_blocked: bool


def _clean(value: str | None, size: int) -> str | None:
    if not value:
        return None
    return value.strip()[:size] or None


def register_telegram_user(
    *,
    telegram_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    language_code: str | None,
    source: str | None = None,
) -> UserInfo:
    """Cria ou atualiza o usuário pelo Telegram ID (UPSERT atômico, sem duplicados)."""
    values = {
        "telegram_id": telegram_id,
        "username": _clean(username, 64),
        "first_name": _clean(first_name, 128),
        "last_name": _clean(last_name, 128),
        "language_code": _clean(language_code, 16),
        "source": _clean(source, 64),
        "last_seen_at": func.now(),
    }
    stmt = pg_insert(User).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[User.telegram_id],
        set_={
            "username": stmt.excluded.username,
            "first_name": stmt.excluded.first_name,
            "last_name": stmt.excluded.last_name,
            "language_code": stmt.excluded.language_code,
            "last_seen_at": func.now(),
            "updated_at": func.now(),
            # mantém a origem do primeiro acesso
            "source": func.coalesce(User.source, stmt.excluded.source),
        },
    ).returning(User.id, User.is_blocked, User.first_name, literal_column("(xmax = 0)").label("inserted"))

    with session_scope() as session:
        row = session.execute(stmt).one()
        if row.inserted:
            log_event(session, "user_registered", user_id=row.id, source=values["source"])
        return UserInfo(
            id=row.id,
            telegram_id=telegram_id,
            first_name=row.first_name,
            is_new=bool(row.inserted),
            is_blocked=bool(row.is_blocked),
        )


def is_blocked(telegram_id: int) -> bool:
    with session_scope() as session:
        return bool(session.scalar(select(User.is_blocked).where(User.telegram_id == telegram_id)))


def get_user_id(telegram_id: int) -> int | None:
    with session_scope() as session:
        return session.scalar(select(User.id).where(User.telegram_id == telegram_id))
