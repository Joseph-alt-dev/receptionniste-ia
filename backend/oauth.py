from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import jwt
import requests
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from google.oauth2.credentials import Credentials as GoogleOAuthCredentials
from googleapiclient.discovery import build as build_google_service
from googleapiclient.errors import HttpError
from loguru import logger
from sqlalchemy.orm import Session

from backend.auth import JWT_ALGORITHM, get_current_compte, get_db
from backend.config import GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, GOOGLE_OAUTH_REDIRECT_URI, JWT_SECRET
from backend.crypto import chiffrer, dechiffrer
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


def _salon_ou_404(salon_id: int, compte: Compte, db: Session) -> Salon:
    # même vérification d'appartenance que le reste de l'API : jamais confiance dans le salon_id du client
    salon = db.get(Salon, salon_id)
    if not salon or salon.compte_id != compte.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Salon introuvable")
    return salon


def _deconnecter_calendrier(salon: Salon, db: Session) -> None:
    """Révoque le jeton chez Google et remet le salon à l'état non connecté.

    Si la révocation échoue côté Google, on supprime quand même notre copie
    locale du jeton : le propriétaire doit pouvoir se reconnecter même si
    Google refuse ou est indisponible.
    """
    if salon.google_refresh_token:
        try:
            reponse = requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": dechiffrer(salon.google_refresh_token)},
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
            if not reponse.ok:
                logger.error(f"Échec de la révocation Google pour le salon {salon.id} : HTTP {reponse.status_code}")
        except requests.RequestException as e:
            logger.error(f"Erreur réseau lors de la révocation Google pour le salon {salon.id} : {e!r}")

    salon.google_refresh_token = None
    salon.google_calendar_id = None
    salon.google_compte_email = None
    salon.calendrier_connecte = False
    db.commit()


@router.get("/authorize")
def authorize(
    salon_id: int,
    reconnexion: bool = False,
    compte: Compte = Depends(get_current_compte),
    db: Session = Depends(get_db),
):
    salon = _salon_ou_404(salon_id, compte, db)
    if reconnexion:
        # "changer de compte" : on coupe l'ancienne connexion avant de relancer Google,
        # et on lui demande explicitement de proposer le choix du compte
        _deconnecter_calendrier(salon, db)

    params = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": CALENDAR_SCOPE,
        "access_type": "offline",
        "prompt": "select_account consent" if reconnexion else "consent",  # consent seul force déjà le refresh_token
        "state": _signer_state(salon.id, compte.id),
    }
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))


@router.post("/deconnecter")
def deconnecter(salon_id: int, compte: Compte = Depends(get_current_compte), db: Session = Depends(get_db)):
    salon = _salon_ou_404(salon_id, compte, db)
    _deconnecter_calendrier(salon, db)
    return {"ok": True, "salon_id": salon.id, "calendrier_connecte": False}


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

    try:
        credentials = GoogleOAuthCredentials(token=tokens["access_token"])
        service = build_google_service("calendar", "v3", credentials=credentials)
        salon.google_compte_email = service.calendars().get(calendarId="primary").execute().get("id")
    except HttpError as e:
        logger.error(f"Impossible de récupérer l'adresse du compte Google connecté pour le salon {salon.id} : {e}")

    db.commit()

    return {"ok": True, "salon_id": salon.id, "calendrier_connecte": True}
