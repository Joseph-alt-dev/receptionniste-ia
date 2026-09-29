import json

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from backend.auth import (
    check_login_rate_limit,
    create_access_token,
    create_refresh_token,
    get_current_compte,
    get_db,
    revoke_refresh_token,
    rotate_refresh_token,
)
from backend.config import ACCESS_TOKEN_EXPIRE_MINUTES, COOKIE_SECURE, CORS_ORIGINS, REFRESH_TOKEN_EXPIRE_DAYS
from backend.models import Compte, Salon
from backend.schemas import LoginRequest, SalonCreate, SalonOut, SalonUpdate, SignupRequest
from backend.security import hash_password, verifier_password

app = FastAPI(title="Réceptionniste IA — API salons")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,  # jamais de wildcard "*" : liste explicite via .env
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
