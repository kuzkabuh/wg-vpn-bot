import os
import time
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from .models import Base, User, Peer
from .settings import SET

os.makedirs(os.path.dirname(SET.database_path), exist_ok=True)
ENGINE = create_engine(f"sqlite:///{SET.database_path}", echo=False, future=True)
SessionLocal = sessionmaker(bind=ENGINE, autoflush=False, autocommit=False, future=True)

Base.metadata.create_all(ENGINE)

# === CRUD helpers ===

def now_ts() -> int:
    return int(time.time())

def get_or_create_user(tg_id: int, username: str|None, first: str|None, last: str|None):
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

def get_user_by_tgid(tg_id: int):
    with SessionLocal() as s:
        return s.execute(select(User).where(User.tg_id == tg_id)).scalar_one_or_none()

def update_user(u: User, **kwargs):
    with SessionLocal() as s:
        u = s.merge(u)
        for k, v in kwargs.items():
            setattr(u, k, v)
        u.updated_at = now_ts()
        s.commit()
        s.refresh(u)
        return u

def list_pending():
    with SessionLocal() as s:
        return s.execute(select(User).where(User.status == "pending")).scalars().all()

def count_user_peers(uid: int) -> int:
    with SessionLocal() as s:
        return s.query(Peer).where(Peer.user_id == uid, Peer.revoked == False).count()

def add_peer_row(user_id: int, interface: str, wgd_peer_id: str, name: str):
    with SessionLocal() as s:
        p = Peer(user_id=user_id, interface=interface, wgd_peer_id=wgd_peer_id, name=name, created_at=now_ts(), revoked=False)
        s.add(p)
        s.commit()
        s.refresh(p)
        return p

def get_user_peers(uid: int):
    with SessionLocal() as s:
        return s.query(Peer).where(Peer.user_id == uid, Peer.revoked == False).all()

def revoke_peer_row(peer_id: int):
    with SessionLocal() as s:
        p = s.query(Peer).get(peer_id)
        if not p:
            return None
        p.revoked = True
        s.commit()
        return p
