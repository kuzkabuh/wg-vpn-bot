# app/models.py
from __future__ import annotations

import time
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ───────────────────────── helpers ─────────────────────────

def now_ts() -> int:
    """UTC unix timestamp (int). Используется как default=callable."""
    return int(time.time())


# ───────────────────────── base ─────────────────────────

class Base(DeclarativeBase):
    pass


# ───────────────────────── models ─────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Telegram
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(64))
    first_name: Mapped[Optional[str]] = mapped_column(String(64))
    last_name: Mapped[Optional[str]] = mapped_column(String(64))

    # Статус: pending / approved / rejected
    status: Mapped[str] = mapped_column(String(16), default="pending")

    # Тариф: none / trial / paid / unlimited
    plan: Mapped[str] = mapped_column(String(16), default="none")
    # -1 => безлимит
    devices_limit: Mapped[int] = mapped_column(Integer, default=0)

    # Срок действия тарифа (UTC, unix ts) — может быть None
    expires_at: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ts)
    updated_at: Mapped[int] = mapped_column(BigInteger, default=now_ts)

    peers: Mapped[List["Peer"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint(
            "status in ('pending','approved','rejected')",
            name="ck_users_status",
        ),
        CheckConstraint(
            "plan in ('none','trial','paid','unlimited')",
            name="ck_users_plan",
        ),
        CheckConstraint("devices_limit >= -1", name="ck_users_devlimit"),
        Index("ix_users_status", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} tg_id={self.tg_id} status={self.status} plan={self.plan}>"


class Peer(Base):
    __tablename__ = "peers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )

    # Имя конфигурации WG (интерфейса), напр. "wg0" или "wg351136125"
    interface: Mapped[str] = mapped_column(String(64), index=True)

    # Идентификатор пира в WGDashboard (как правило, publicKey)
    wgd_peer_id: Mapped[str] = mapped_column(String(128), index=True)

    # Человекочитаемое имя пира
    name: Mapped[str] = mapped_column(String(128))

    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ts)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    user: Mapped["User"] = relationship(back_populates="peers")

    __table_args__ = (
        # Не допускаем дубли активного пира на одного пользователя в одной конфигурации
        UniqueConstraint(
            "user_id",
            "interface",
            "wgd_peer_id",
            "revoked",
            name="uq_peers_user_interface_peer_revoked",
        ),
        Index("ix_peers_user_interface", "user_id", "interface"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        state = "revoked" if self.revoked else "active"
        return f"<Peer id={self.id} user_id={self.user_id} {self.interface} {self.wgd_peer_id} {state}>"
