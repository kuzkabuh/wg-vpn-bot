from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy import String, Integer, BigInteger, Boolean, ForeignKey
from typing import Optional, List

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(64))
    first_name: Mapped[Optional[str]] = mapped_column(String(64))
    last_name: Mapped[Optional[str]] = mapped_column(String(64))

    # status: pending/approved/rejected
    status: Mapped[str] = mapped_column(String(16), default="pending")

    # plan: none/trial/paid/unlimited
    plan: Mapped[str] = mapped_column(String(16), default="none")
    devices_limit: Mapped[int] = mapped_column(Integer, default=0)  # -1 => безлимит

    expires_at: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True) # unix ts
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)

    peers: Mapped[List["Peer"]] = relationship(back_populates="user", cascade="all, delete-orphan")

class Peer(Base):
    __tablename__ = "peers"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    interface: Mapped[str] = mapped_column(String(32))
    wgd_peer_id: Mapped[str] = mapped_column(String(128), index=True)  # ID в WGDashboard
    name: Mapped[str] = mapped_column(String(128))

    created_at: Mapped[int] = mapped_column(BigInteger)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped["User"] = relationship(back_populates="peers")
