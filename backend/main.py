import json
import mimetypes
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from backend.auth import (
    check_login_rate_limit,
    create_access_token,
    create_refresh_token,
    get_current_admin,
    get_current_compte,
    get_db,
    revoke_all_refresh_tokens,
    revoke_refresh_token,
    rotate_refresh_token,
)
from backend.config import ACCESS_TOKEN_EXPIRE_MINUTES, COOKIE_SECURE, CORS_ORIGINS, REFRESH_TOKEN_EXPIRE_DAYS
from backend.models import Compte, RefreshToken, Salon
from backend.oauth import router as oauth_router
from backend.schemas import CompteAdminOut, LoginRequest, SalonCreate, SalonOut, SalonUpdate, SignupRequest
from backend.security import hash_password, verifier_password

# le mime.types du système peut ne pas déclarer .css/.js (varie selon l'OS/le
# déploiement) ; Safari refuse d'appliquer un CSS sans Content-Type text/css
# correct, contrairement à Chrome qui est tolérant — on force le mapping.
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/javascript", ".js")

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

app = FastAPI(title="Réceptionniste IA — API salons")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,  # jamais de wildcard "*" : liste explicite via .env
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(oauth_router)


def _set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    response.set_cookie(
        "access_token",
        access_token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    response.set_cookie(
        "refresh_token",
        refresh_token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600,
        path="/api/auth",  # envoyé uniquement aux routes d'auth, pas à toute l'API
    )


@app.post("/api/auth/signup", status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest, response: Response, db: Session = Depends(get_db)):
    if db.query(Compte).filter_by(email=payload.email).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Un compte existe déjà avec cet email")
    compte = Compte(email=payload.email, mot_de_passe_hash=hash_password(payload.mot_de_passe))
    db.add(compte)
    db.commit()
    access_token = create_access_token(compte.id)
    refresh_token = create_refresh_token(compte.id, db)
    _set_auth_cookies(response, access_token, refresh_token)
    return {"email": compte.email}


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    check_login_rate_limit(request.client.host)
    compte = db.query(Compte).filter_by(email=payload.email).first()
    if not compte or not verifier_password(payload.mot_de_passe, compte.mot_de_passe_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Email ou mot de passe incorrect")
    access_token = create_access_token(compte.id)
    refresh_token = create_refresh_token(compte.id, db)
    _set_auth_cookies(response, access_token, refresh_token)
    return {"email": compte.email}


@app.post("/api/auth/refresh")
def refresh(request: Request, response: Response, db: Session = Depends(get_db)):
    raw_token = request.cookies.get("refresh_token")
    result = rotate_refresh_token(raw_token, db) if raw_token else None
    if not result:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expirée")
    access_token, new_refresh_token = result
    _set_auth_cookies(response, access_token, new_refresh_token)
    return {"ok": True}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    raw_token = request.cookies.get("refresh_token")
    if raw_token:
        revoke_refresh_token(raw_token, db)
    response.delete_cookie("access_token")
    response.delete_cookie("refresh_token", path="/api/auth")
    return {"ok": True}


def _salon_to_dict(salon: Salon) -> dict:
    return {
        "id": salon.id,
        "nom": salon.nom,
        "adresse": salon.adresse,
        "numero_twilio": salon.numero_twilio,
        "est_demo": salon.est_demo,
        "calendrier_connecte": salon.calendrier_connecte,
        "horaires": json.loads(salon.horaires),
        "fermetures_exceptionnelles": json.loads(salon.fermetures_exceptionnelles),
        "prestations": json.loads(salon.prestations),
    }


def _get_owned_salon(salon_id: int, compte: Compte, db: Session) -> Salon:
    # vérification systématique de l'appartenance : jamais confiance dans le salon_id envoyé par le client
    salon = db.get(Salon, salon_id)
    if not salon or salon.compte_id != compte.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Salon introuvable")
    return salon


@app.get("/api/salons", response_model=list[SalonOut])
def lister_salons(compte: Compte = Depends(get_current_compte), db: Session = Depends(get_db)):
    salons = db.query(Salon).filter_by(compte_id=compte.id).all()
    return [_salon_to_dict(s) for s in salons]


@app.post("/api/salons", response_model=SalonOut, status_code=status.HTTP_201_CREATED)
def creer_salon(payload: SalonCreate, compte: Compte = Depends(get_current_compte), db: Session = Depends(get_db)):
    salon = Salon(
        compte_id=compte.id,
        nom=payload.nom,
        adresse=payload.adresse,
        google_calendar_id="",  # pas encore de champ dédié dans le dashboard, voir étape 4
        horaires=payload.horaires.model_dump_json(),
        fermetures_exceptionnelles=json.dumps([d.isoformat() for d in payload.fermetures_exceptionnelles]),
        prestations=payload.prestations.model_dump_json(),
    )
    db.add(salon)
    db.commit()
    return _salon_to_dict(salon)


@app.get("/api/salons/{salon_id}", response_model=SalonOut)
def lire_salon(salon_id: int, compte: Compte = Depends(get_current_compte), db: Session = Depends(get_db)):
    return _salon_to_dict(_get_owned_salon(salon_id, compte, db))


@app.put("/api/salons/{salon_id}", response_model=SalonOut)
def modifier_salon(
    salon_id: int,
    payload: SalonUpdate,
    compte: Compte = Depends(get_current_compte),
    db: Session = Depends(get_db),
):
    salon = _get_owned_salon(salon_id, compte, db)
    salon.nom = payload.nom
    salon.adresse = payload.adresse
    salon.horaires = payload.horaires.model_dump_json()
    salon.fermetures_exceptionnelles = json.dumps([d.isoformat() for d in payload.fermetures_exceptionnelles])
    salon.prestations = payload.prestations.model_dump_json()
    db.commit()
    return _salon_to_dict(salon)


@app.get("/admin/comptes", response_model=list[CompteAdminOut])
def admin_lister_comptes(admin: Compte = Depends(get_current_admin), db: Session = Depends(get_db)):
    comptes = db.query(Compte).all()
    return [
        {
            "id": c.id,
            "email": c.email,
            "actif": c.actif,
            "est_admin": c.est_admin,
            "salons": [{"id": s.id, "nom": s.nom, "est_demo": s.est_demo} for s in c.salons],
        }
        for c in comptes
    ]


@app.post("/admin/comptes/{compte_id}/suspendre")
def admin_suspendre_compte(compte_id: int, admin: Compte = Depends(get_current_admin), db: Session = Depends(get_db)):
    compte = db.get(Compte, compte_id)
    if not compte:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Compte introuvable")
    compte.actif = False
    db.commit()
    revoke_all_refresh_tokens(compte_id, db)
    return {"ok": True}


@app.post("/admin/comptes/{compte_id}/reactiver")
def admin_reactiver_compte(compte_id: int, admin: Compte = Depends(get_current_admin), db: Session = Depends(get_db)):
    compte = db.get(Compte, compte_id)
    if not compte:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Compte introuvable")
    compte.actif = True
    db.commit()
    return {"ok": True}


@app.post("/admin/comptes/{compte_id}/supprimer")
def admin_supprimer_compte(compte_id: int, admin: Compte = Depends(get_current_admin), db: Session = Depends(get_db)):
    if compte_id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Impossible de supprimer son propre compte")
    compte = db.get(Compte, compte_id)
    if not compte:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Compte introuvable")
    # pas de cascade configurée sur les relations : on supprime d'abord les lignes dépendantes
    db.query(RefreshToken).filter_by(compte_id=compte_id).delete()
    db.query(Salon).filter_by(compte_id=compte_id).delete()
    db.delete(compte)
    db.commit()
    return {"ok": True}


# montés en dernier : les routes explicites ci-dessus restent prioritaires sur ces catch-all
app.mount("/dashboard-admin", StaticFiles(directory=FRONTEND_DIR / "admin", html=True), name="dashboard_admin")
app.mount("/site", StaticFiles(directory=FRONTEND_DIR / "site", html=True), name="site")
