"""Vários clientes (contas) no mesmo sistema, cada um vendo SÓ os próprios dados.

Como funciona:
- Toda tabela de negócio tem `account_id` (mixin AccountScoped).
- A conta "da vez" fica numa ContextVar:
    * no painel, o middleware põe a conta do administrador logado;
    * nos processos dos bots, cada bot roda dentro de `use_account(bot.account_id)`.
- Com uma conta definida, TODA consulta do ORM (select, update, delete, session.get)
  recebe automaticamente `WHERE account_id = <conta>` — não depende de lembrar o filtro.
- Registros novos recebem o `account_id` da conta da vez; sem conta definida, ele é
  deduzido do "pai" (ex.: o post herda a conta do bot). Sem nenhuma das duas, o banco
  recusa (account_id é obrigatório) — nunca grava dado "sem dono".
- Sem conta definida (rotinas da plataforma, migrações), nada é filtrado.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import ForeignKey, Integer, event
from sqlalchemy.orm import Mapped, ORMExecuteState, Session, declared_attr, mapped_column, with_loader_criteria

_current: ContextVar[int | None] = ContextVar("current_account_id", default=None)
ALL_ACCOUNTS = "all_accounts"      # execution option: consulta sem o filtro (telas da plataforma)


class AccountScoped:
    """Mixin: a linha pertence a uma conta (cliente)."""

    @declared_attr
    def account_id(cls) -> Mapped[int]:  # noqa: N805
        return mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)


# Conta principal (dona da plataforma). Enquanto o bot de vendas não é por conta, é a conta dele.
PRIMARY_ACCOUNT_ID = 1


def current_account_id() -> int | None:
    return _current.get()


def account_or_primary() -> int:
    return _current.get() or PRIMARY_ACCOUNT_ID


def set_account(account_id: int | None):
    """Define a conta da vez; devolve o token para `reset`."""
    return _current.set(account_id)


def reset(token) -> None:
    _current.reset(token)


@contextmanager
def use_account(account_id: int | None) -> Iterator[None]:
    token = _current.set(account_id)
    try:
        yield
    finally:
        _current.reset(token)


@contextmanager
def unscoped() -> Iterator[None]:
    """Sem filtro de conta (somente telas/rotinas da PLATAFORMA)."""
    with use_account(None):
        yield


# Como descobrir a conta de um registro novo pelo "pai" (quando não há conta da vez)
_PARENTS: dict[str, list[tuple[str, str]]] = {}


def inherit_from(model_name: str, *pairs: tuple[str, str]) -> None:
    """inherit_from("OfferPost", ("bot_id", "OfferBot")) — o post herda a conta do bot."""
    _PARENTS[model_name] = list(pairs)


@event.listens_for(Session, "do_orm_execute")
def _scope_queries(state: ORMExecuteState) -> None:
    account_id = _current.get()
    if account_id is None or state.execution_options.get(ALL_ACCOUNTS):
        return
    if not (state.is_select or state.is_update or state.is_delete):
        return
    if state.is_column_load or state.is_relationship_load:
        # carregamentos de atributos/relacionamentos de objetos que já passaram pelo filtro
        return
    state.statement = state.statement.options(
        with_loader_criteria(AccountScoped, lambda cls: cls.account_id == account_id, include_aliases=True)
    )


@event.listens_for(Session, "before_flush")
def _stamp_new_rows(session: Session, flush_context, instances) -> None:
    account_id = _current.get()
    for obj in session.new:
        if not isinstance(obj, AccountScoped) or obj.account_id is not None:
            continue
        obj.account_id = account_id if account_id is not None else _from_parent(session, obj)


def _from_parent(session: Session, obj) -> int | None:
    mappers = {m.class_.__name__: m.class_ for m in type(obj).registry.mappers}
    for attr, parent_name in _PARENTS.get(type(obj).__name__, []):
        parent_id = getattr(obj, attr, None)
        if parent_id is None:
            continue
        parent_cls = mappers.get(parent_name)
        if parent_cls is None:
            continue
        with session.no_autoflush:
            parent = session.get(parent_cls, parent_id, execution_options={ALL_ACCOUNTS: True})
        if parent is not None:
            return parent.account_id
    return None
