import hashlib
import secrets
import time
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.config import ACCESS_TOKEN_EXPIRE_MINUTES, JWT_SECRET, REFRESH_TOKEN_EXPIRE_DAYS
from backend.database import SessionLocal
from backend.models import Compte, RefreshToken

JWT_ALGORITHM = "HS256"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _utcnow_naive() -> datetime:
    # les colonnes DateTime de SQLite sont naïves : on reste naïf partout côté DB
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_access_token(compte_id: int) -> str:
    payload = {
        "sub": str(compte_id),
        "type": "access",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_refresh_token(compte_id: int, db: Session) -> str:
    raw_token = secrets.token_urlsafe(32)
    db.add(
        RefreshToken(
            compte_id=compte_id,
            token_hash=_hash_token(raw_token),
            expire_le=_utcnow_naive() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        )
    )
    db.commit()
    return raw_token


def revoke_refresh_token(raw_token: str, db: Session) -> None:
    entry = db.query(RefreshToken).filter_by(token_hash=_hash_token(raw_token)).first()
    if entry:
        entry.revoque = True
        db.commit()


def rotate_refresh_token(raw_token: str, db: Session) -> tuple[str, str] | None:
    """Vérifie un refresh token, le révoque, et en émet un nouveau couple (rotation).
    Retourne None si le token est invalide, révoqué ou expiré.
    """
    entry = db.query(RefreshToken).filter_by(token_hash=_hash_token(raw_token)).first()
    if not entry or entry.revoque or entry.expire_le < _utcnow_naive():
        return None
    entry.revoque = True
    db.commit()
    return create_access_token(entry.compte_id), create_refresh_token(entry.compte_id, db)


def get_current_compte(
    access_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> Compte:
    if not access_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Non authentifié")
    try:
        payload = jwt.decode(access_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session invalide ou expirée")
    if payload.get("type") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session invalide")
    compte = db.get(Compte, int(payload["sub"]))
    if not compte:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Compte introuvable")
    return compte


# ponytail: limiteur en mémoire, mono-process — suffisant pour le MVP hackathon.
# À remplacer par un store partagé (Redis) si un jour plusieurs instances tournent derrière un load balancer.
_login_attempts: dict[str, list[float]] = {}
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 60


def check_login_rate_limit(ip: str) -> None:
    now = time.time()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
    if len(attempts) >= LOGIN_MAX_ATTEMPTS:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Trop de tentatives, réessayez plus tard")
    attempts.append(now)
    _login_attempts[ip] = attempts
