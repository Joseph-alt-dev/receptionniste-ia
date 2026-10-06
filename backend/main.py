import datetime as dt
import difflib
import hashlib
import io
import json
import mimetypes
import re
import unicodedata
import uuid
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.adresses import composer_adresse
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
from backend.geocodage import geocoder_adresse, geocoder_ville_ou_code_postal
from backend.models import Avis, Compte, RefreshToken, Salon
from backend.oauth import router as oauth_router
from backend.schemas import (
    AvisCreate,
    AvisOut,
    CompteAdminOut,
    LoginRequest,
    SalonCreate,
    SalonOut,
    SalonRechercheResultat,
    SalonUpdate,
    SignupRequest,
)
from backend.security import hash_password, verifier_password
from backend.slugs import generer_slug
from salon_bot import ConversationTexte

# le mime.types du système peut ne pas déclarer .css/.js (varie selon l'OS/le
# déploiement) ; Safari refuse d'appliquer un CSS sans Content-Type text/css
# correct, contrairement à Chrome qui est tolérant — on force le mapping.
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/javascript", ".js")

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
UPLOADS_DIR = Path(__file__).parent.parent / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
PHOTOS_MAX = 5
PHOTO_TAILLE_MAX = 5 * 1024 * 1024  # 5 Mo
PHOTO_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}  # format Pillow -> extension

app = FastAPI(title="Réceptionniste IA — API salons")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,  # jamais de wildcard "*" : liste explicite via .env
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(oauth_router)


@app.middleware("http")
async def desactiver_cache_en_dev(request: Request, call_next):
    response = await call_next(request)
    if not COOKIE_SECURE:  # dev (http local) : jamais de cache navigateur, pour éviter qu'un vieux style.css/JS reste coincé dans le cache de Safari
        response.headers["Cache-Control"] = "no-cache"
    return response


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


def _avis_stats(salon_id: int, db: Session) -> tuple[float | None, int]:
    moyenne, nombre = db.query(func.avg(Avis.note), func.count(Avis.id)).filter_by(salon_id=salon_id).one()
    return (round(moyenne, 1) if moyenne is not None else None), nombre


def _salon_to_dict(salon: Salon, db: Session) -> dict:
    note_moyenne, nombre_avis = _avis_stats(salon.id, db)
    return {
        "id": salon.id,
        "nom": salon.nom,
        "slug": salon.slug,
        "numero_et_rue": salon.numero_et_rue,
        "complement": salon.complement,
        "code_postal": salon.code_postal,
        "ville": salon.ville,
        "departement": salon.departement,
        "pays": salon.pays,
        "description": salon.description,
        "adresse_complete": composer_adresse(salon.numero_et_rue, salon.complement, salon.code_postal, salon.ville, salon.pays),
        "latitude": salon.latitude,
        "longitude": salon.longitude,
        "numero_twilio": salon.numero_twilio,
        "est_demo": salon.est_demo,
        "calendrier_connecte": salon.calendrier_connecte,
        "horaires": json.loads(salon.horaires),
        "fermetures_exceptionnelles": json.loads(salon.fermetures_exceptionnelles),
        "prestations": json.loads(salon.prestations),
        "photos": json.loads(salon.photos),
        "note_moyenne": note_moyenne,
        "nombre_avis": nombre_avis,
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
    return [_salon_to_dict(s, db) for s in salons]


@app.post("/api/salons", response_model=SalonOut, status_code=status.HTTP_201_CREATED)
def creer_salon(payload: SalonCreate, compte: Compte = Depends(get_current_compte), db: Session = Depends(get_db)):
    latitude, longitude = geocoder_adresse(payload.numero_et_rue, payload.code_postal, payload.ville, payload.pays)
    salon = Salon(
        compte_id=compte.id,
        nom=payload.nom,
        slug=generer_slug(payload.nom, db),
        numero_et_rue=payload.numero_et_rue,
        complement=payload.complement,
        code_postal=payload.code_postal,
        ville=payload.ville,
        departement=payload.departement,
        pays=payload.pays,
        description=payload.description,
        latitude=latitude,
        longitude=longitude,
        google_calendar_id="",  # pas encore de champ dédié dans le dashboard, voir étape 4
        horaires=payload.horaires.model_dump_json(),
        fermetures_exceptionnelles=json.dumps([d.isoformat() for d in payload.fermetures_exceptionnelles]),
        prestations=payload.prestations.model_dump_json(),
    )
    db.add(salon)
    db.commit()
    return _salon_to_dict(salon, db)


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rayon_terre_km = 6371
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * rayon_terre_km * asin(sqrt(a))


def _salon_complet(salon: Salon) -> bool:
    """Un salon n'est listé publiquement que s'il a de quoi afficher une fiche utile."""
    adresse_ok = bool(salon.numero_et_rue and salon.code_postal and salon.ville)
    horaires_ok = any(json.loads(salon.horaires).values())
    prestations_ok = any(items for items in json.loads(salon.prestations).values())
    return adresse_ok and horaires_ok and prestations_ok


def _normaliser(texte: str) -> str:
    sans_accents = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode("ascii")
    return sans_accents.lower().strip()


def _score_champ(texte_normalise: str, champ: str | None) -> float:
    """0..1 : correspondance exacte/partielle en priorité, sinon similarité
    tolérante à 1-2 fautes de frappe (difflib)."""
    if not champ:
        return 0.0
    champ_normalise = _normaliser(champ)
    if texte_normalise in champ_normalise:
        return 1.0
    ratio = difflib.SequenceMatcher(None, texte_normalise, champ_normalise).ratio()
    for mot in champ_normalise.split():
        ratio = max(ratio, difflib.SequenceMatcher(None, texte_normalise, mot).ratio())
    return ratio


def _score_pertinence(texte_normalise: str, salon: Salon) -> tuple[float, float, float]:
    """(score_nom, score_ville, score_code_postal) : tri par pertinence =
    meilleure correspondance du nom d'abord, puis ville, puis code postal."""
    return (
        _score_champ(texte_normalise, salon.nom),
        _score_champ(texte_normalise, salon.ville),
        _score_champ(texte_normalise, salon.code_postal),
    )


SEUIL_PERTINENCE = 0.6  # ponytail: tolère ~1-2 fautes de frappe sur un nom de salon


@app.get("/api/geocoder")
def geocoder(q: str):
    """Géocodage ponctuel d'une saisie libre "ville ou code postal" pour la
    recherche "près de vous" côté client. Rien n'est stocké côté serveur :
    la position est gardée par le navigateur (sessionStorage)."""
    latitude, longitude = geocoder_ville_ou_code_postal(q)
    return {"latitude": latitude, "longitude": longitude}


@app.get("/api/salons/recherche", response_model=list[SalonRechercheResultat])
def rechercher_salons(
    texte: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    tri: str | None = None,
    db: Session = Depends(get_db),
):
    salons = [s for s in db.query(Salon).order_by(Salon.id.desc()).all() if _salon_complet(s)]

    texte_normalise = _normaliser(texte) if texte else None
    scores = {s.id: _score_pertinence(texte_normalise, s) for s in salons} if texte_normalise else {}
    if texte_normalise:
        salons = [s for s in salons if max(scores[s.id]) >= SEUIL_PERTINENCE]

    resultats = []
    for salon in salons:
        note_moyenne, nombre_avis = _avis_stats(salon.id, db)
        photos = json.loads(salon.photos)
        distance_km = None
        if latitude is not None and longitude is not None and salon.latitude is not None and salon.longitude is not None:
            distance_km = round(_distance_km(latitude, longitude, salon.latitude, salon.longitude), 1)
        resultats.append({
            "nom": salon.nom,
            "slug": salon.slug,
            "ville": salon.ville,
            "note_moyenne": note_moyenne,
            "nombre_avis": nombre_avis,
            "photo_principale": f"/uploads/salons/{salon.id}/{photos[0]}" if photos else None,
            "distance_km": distance_km,
            "_pertinence": scores.get(salon.id, (0.0, 0.0, 0.0)),
        })

    tri_effectif = tri or ("distance" if latitude is not None and longitude is not None else "pertinence" if texte_normalise else "note")
    if tri_effectif == "pertinence":
        resultats.sort(key=lambda r: tuple(-x for x in r["_pertinence"]))
    elif tri_effectif == "note":
        resultats.sort(key=lambda r: (r["note_moyenne"] is None, -(r["note_moyenne"] or 0)))
    elif tri_effectif == "distance":
        resultats.sort(key=lambda r: (r["distance_km"] is None, r["distance_km"] or 0))
    # tri == "recent" : déjà dans le bon ordre (order_by appliqué à la requête ci-dessus)

    return resultats



@app.get("/api/salons/{salon_id}", response_model=SalonOut)
def lire_salon(salon_id: int, compte: Compte = Depends(get_current_compte), db: Session = Depends(get_db)):
    return _salon_to_dict(_get_owned_salon(salon_id, compte, db), db)


@app.put("/api/salons/{salon_id}", response_model=SalonOut)
def modifier_salon(
    salon_id: int,
    payload: SalonUpdate,
    compte: Compte = Depends(get_current_compte),
    db: Session = Depends(get_db),
):
    salon = _get_owned_salon(salon_id, compte, db)
    adresse_changee = (
        payload.numero_et_rue != salon.numero_et_rue
        or payload.code_postal != salon.code_postal
        or payload.ville != salon.ville
        or payload.pays != salon.pays
    )
    salon.nom = payload.nom
    salon.numero_et_rue = payload.numero_et_rue
    salon.complement = payload.complement
    salon.code_postal = payload.code_postal
    salon.ville = payload.ville
    salon.departement = payload.departement
    salon.pays = payload.pays
    salon.description = payload.description
    if adresse_changee:
        salon.latitude, salon.longitude = geocoder_adresse(payload.numero_et_rue, payload.code_postal, payload.ville, payload.pays)
    salon.horaires = payload.horaires.model_dump_json()
    salon.fermetures_exceptionnelles = json.dumps([d.isoformat() for d in payload.fermetures_exceptionnelles])
    salon.prestations = payload.prestations.model_dump_json()
    db.commit()
    return _salon_to_dict(salon, db)


@app.post("/api/salons/{salon_id}/photos", response_model=SalonOut)
async def ajouter_photo(
    salon_id: int,
    fichier: UploadFile,
    compte: Compte = Depends(get_current_compte),
    db: Session = Depends(get_db),
):
    salon = _get_owned_salon(salon_id, compte, db)
    photos = json.loads(salon.photos)
    if len(photos) >= PHOTOS_MAX:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Maximum {PHOTOS_MAX} photos par salon")

    contenu = await fichier.read()
    if len(contenu) > PHOTO_TAILLE_MAX:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Photo trop lourde (5 Mo maximum)")

    try:
        image = Image.open(io.BytesIO(contenu))
        format_image = image.format  # lu avant verify() : l'image n'est plus utilisable après
        image.verify()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Fichier image invalide")

    extension = PHOTO_FORMATS.get(format_image)
    if not extension:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Formats acceptés : JPEG, PNG, WEBP")

    dossier = UPLOADS_DIR / "salons" / str(salon_id)
    dossier.mkdir(parents=True, exist_ok=True)
    # nom unique à chaque upload : évite qu'un remplacement de photo serve une
    # version mise en cache par le navigateur du client sous le même nom
    nom_fichier = f"{uuid.uuid4().hex}{extension}"
    (dossier / nom_fichier).write_bytes(contenu)

    photos.append(nom_fichier)
    salon.photos = json.dumps(photos)
    db.commit()
    return _salon_to_dict(salon, db)


@app.delete("/api/salons/{salon_id}/photos/{nom_fichier}", response_model=SalonOut)
def retirer_photo(
    salon_id: int,
    nom_fichier: str,
    compte: Compte = Depends(get_current_compte),
    db: Session = Depends(get_db),
):
    salon = _get_owned_salon(salon_id, compte, db)
    photos = json.loads(salon.photos)
    if nom_fichier not in photos:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Photo introuvable")
    photos.remove(nom_fichier)
    salon.photos = json.dumps(photos)
    db.commit()
    (UPLOADS_DIR / "salons" / str(salon_id) / nom_fichier).unlink(missing_ok=True)
    return _salon_to_dict(salon, db)


@app.get("/api/public/salons/{slug}")
def lire_salon_public(slug: str, db: Session = Depends(get_db)):
    # page vitrine : lecture directe en base à chaque requête, aucune copie/cache
    salon = db.query(Salon).filter_by(slug=slug).first()
    if not salon:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Salon introuvable")
    return _salon_to_dict(salon, db)


def _hash_ip(ip: str) -> str:
    return hashlib.sha256(ip.encode()).hexdigest()


@app.get("/api/public/salons/{slug}/avis", response_model=list[AvisOut])
def lister_avis(slug: str, db: Session = Depends(get_db)):
    salon = db.query(Salon).filter_by(slug=slug).first()
    if not salon:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Salon introuvable")
    return db.query(Avis).filter_by(salon_id=salon.id).order_by(Avis.cree_le.desc()).all()


@app.post("/api/public/salons/{slug}/avis", response_model=AvisOut, status_code=status.HTTP_201_CREATED)
def deposer_avis(slug: str, payload: AvisCreate, request: Request, db: Session = Depends(get_db)):
    salon = db.query(Salon).filter_by(slug=slug).first()
    if not salon:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Salon introuvable")

    ip_hash = _hash_ip(request.client.host)
    depuis_24h = dt.datetime.utcnow() - dt.timedelta(days=1)
    deja_depose = (
        db.query(Avis)
        .filter(Avis.salon_id == salon.id, Avis.ip_hash == ip_hash, Avis.cree_le >= depuis_24h)
        .first()
    )
    if deja_depose:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Un seul avis par jour pour ce salon")

    commentaire = re.sub(r"[\x00-\x1f\x7f]", "", payload.commentaire).strip() if payload.commentaire else None
    avis = Avis(salon_id=salon.id, note=payload.note, commentaire=commentaire or None, ip_hash=ip_hash)
    db.add(avis)
    db.commit()
    return avis


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


@app.websocket("/chat/ws")
async def chat_ws(websocket: WebSocket, salon_id: int, db: Session = Depends(get_db)):
    salon = db.get(Salon, salon_id)
    await websocket.accept()
    if not salon:
        await websocket.send_json({"role": "error", "content": "Salon introuvable."})
        await websocket.close()
        return
    db.expunge(salon)

    # une conversation par connexion : aucune mémoire partagée entre deux clients
    conversation = ConversationTexte(salon, canal="chat")
    try:
        await websocket.send_json({"role": "info", "nom_salon": salon.nom})
        reponse = await conversation.tour("[Le client vient d'ouvrir le chat. Présente-toi brièvement.]")
        await websocket.send_json({"role": "assistant", "content": reponse})
        while True:
            data = await websocket.receive_json()
            texte = (data.get("message") or "").strip()
            if not texte:
                continue
            reponse = await conversation.tour(texte)
            await websocket.send_json({"role": "assistant", "content": reponse})
    except WebSocketDisconnect:
        pass


@app.get("/")
def page_accueil():
    return FileResponse(FRONTEND_DIR / "site" / "accueil.html")


@app.get("/salon/{slug}")
def page_salon_public(slug: str):
    # même page statique pour tous les salons : le slug est lu et résolu côté
    # client via /api/public/salons/{slug} (pas de template serveur)
    return FileResponse(FRONTEND_DIR / "site" / "salon-public.html")


# montés en dernier : les routes explicites ci-dessus restent prioritaires sur ces catch-all
app.mount("/dashboard-admin", StaticFiles(directory=FRONTEND_DIR / "admin", html=True), name="dashboard_admin")
app.mount("/site", StaticFiles(directory=FRONTEND_DIR / "site", html=True), name="site")
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")
