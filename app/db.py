import os
import time
from typing import Optional, List

from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker

from .models import Base, User, Peer
from .settings import SET

# --- Storage init ----------------------------------------------------------------

db_dir = os.path.dirname(SET.database_path or "")
if db_dir:
    os.makedirs(db_dir, exist_ok=True)

# check_same_thread=False — полезно для uvicorn/aiogram (разные потоки)
ENGINE = create_engine(
    f"sqlite:///{SET.database_path}",
    echo=False,
    future=True,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=ENGINE, autoflush=False, autocommit=False, future=True)

Base.metadata.create_all(ENGINE)

# --- Helpers ---------------------------------------------------------------------

def now_ts() -> int:
    return int(time.time())

# --- Users -----------------------------------------------------------------------

def get_or_create_user(tg_id: int, username: Optional[str], first: Optional[str], last: Optional[str]) -> User:
    with SessionLocal() as s:
        u = s.execute(select(User).where(User.tg_id == tg_id)).scalar_one_or_none()
        if u:
            u.username = username
            u.first_name = first
            u.last_name = last
            u.updated_at = now_ts()
            s.commit()
            s.refresh(u)
            return u

        u = User(
            tg_id=tg_id,
            username=username,
            first_name=first,
            last_name=last,
            status="pending",
            plan="none",
            devices_limit=0,
            expires_at=None,
            created_at=now_ts(),
            updated_at=now_ts(),
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        return u

def get_user_by_tgid(tg_id: int) -> Optional[User]:
    with SessionLocal() as s:
        return s.execute(select(User).where(User.tg_id == tg_id)).scalar_one_or_none()

def update_user(u: User, **kwargs) -> User:
    with SessionLocal() as s:
        u = s.merge(u)
        for k, v in kwargs.items():
            setattr(u, k, v)
        u.updated_at = now_ts()
        s.commit()
        s.refresh(u)
        return u

def list_pending() -> List[User]:
    with SessionLocal() as s:
        return s.execute(select(User).where(User.status == "pending")).scalars().all()

# --- Peers -----------------------------------------------------------------------

def count_user_peers(uid: int) -> int:
    with SessionLocal() as s:
        result = s.execute(
            select(func.count(Peer.id)).where(Peer.user_id == uid, Peer.revoked == False)  # noqa: E712
        ).scalar_one()
        return int(result or 0)

def add_peer_row(user_id: int, interface: str, wgd_peer_id: str, name: str) -> Peer:
    with SessionLocal() as s:
        p = Peer(
            user_id=user_id,
            interface=interface,
            wgd_peer_id=str(wgd_peer_id),
            name=name,
            created_at=now_ts(),
            revoked=False,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        return p

def get_user_peers(uid: int) -> List[Peer]:
    """Активные пиры пользователя, отсортированы по времени создания, затем по id."""
    with SessionLocal() as s:
        stmt = (
            select(Peer)
            .where(Peer.user_id == uid, Peer.revoked == False)  # noqa: E712
            .order_by(Peer.created_at.asc(), Peer.id.asc())
        )
        return s.execute(stmt).scalars().all()

def revoke_peer_row(peer_id: int) -> Optional[Peer]:
    with SessionLocal() as s:
        p = s.get(Peer, peer_id)
        if not p:
            return None
        p.revoked = True
        s.commit()
        s.refresh(p)
        return p

def rename_peer_row(peer_id: int, new_name: str) -> Optional[Peer]:
    """
    Локальное переименование пира (только в БД бота; WGDashboard не трогаем).
    Используется в user.py для кнопки «✏️ Имя».
    """
    new_name = (new_name or "").strip()
    if not new_name:
        return None
    with SessionLocal() as s:
        p = s.get(Peer, peer_id)
        if not p:
            return None
        p.name = new_name
        s.commit()
        s.refresh(p)
        return p
