from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import jwt
import requests
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from backend.auth import JWT_ALGORITHM, get_current_compte, get_db
from backend.config import GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, GOOGLE_OAUTH_REDIRECT_URI, JWT_SECRET
from backend.crypto import chiffrer
from backend.models import Compte, Salon

router = APIRouter(prefix="/api/oauth/google", tags=["oauth"])

STATE_EXPIRE_MINUTES = 10
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"


def _signer_state(salon_id: int, compte_id: int) -> str:
    payload = {
        "type": "oauth_state",  # empêche qu'un access/refresh token soit rejoué ici (voir get_current_compte)
        "salon_id": salon_id,
        "compte_id": compte_id,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=STATE_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _verifier_state(state: str) -> dict:
    try:
        payload = jwt.decode(state, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "State OAuth invalide ou expiré")
    if payload.get("type") != "oauth_state":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "State OAuth invalide")
    return payload


@router.get("/authorize")
def authorize(salon_id: int, compte: Compte = Depends(get_current_compte), db: Session = Depends(get_db)):
    # même vérification d'appartenance que le reste de l'API : jamais confiance dans le salon_id du client
    salon = db.get(Salon, salon_id)
    if not salon or salon.compte_id != compte.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Salon introuvable")

    params = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": CALENDAR_SCOPE,
        "access_type": "offline",
        "prompt": "consent",  # force le renvoi d'un refresh_token même si déjà consenti par le passé
        "state": _signer_state(salon.id, compte.id),
    }
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))


@router.get("/callback")
def callback(code: str, state: str, db: Session = Depends(get_db)):
    # le state signé remplace la session ici : le cookie SameSite=Strict n'est pas envoyé
    # par le navigateur sur cette redirection cross-site depuis accounts.google.com
    payload = _verifier_state(state)

    salon = db.get(Salon, payload["salon_id"])
    if not salon or salon.compte_id != payload["compte_id"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Salon introuvable ou propriétaire différent")

    reponse = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
            "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=10,
    )
    tokens = reponse.json() if reponse.ok else {}
    if not reponse.ok or "refresh_token" not in tokens:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Échec de la connexion à Google Calendar")

    salon.google_refresh_token = chiffrer(tokens["refresh_token"])
    salon.google_calendar_id = "primary"  # alias Google : calendrier principal du compte qui vient de se connecter
    salon.calendrier_connecte = True
    db.commit()

    return {"ok": True, "salon_id": salon.id, "calendrier_connecte": True}
