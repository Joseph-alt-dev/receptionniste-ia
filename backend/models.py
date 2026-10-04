from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import relationship

from backend.database import Base


class Compte(Base):
    __tablename__ = "comptes"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    mot_de_passe_hash = Column(String(255), nullable=False)
    est_admin = Column(Boolean, nullable=False, default=False)
    actif = Column(Boolean, nullable=False, default=True)
    cree_le = Column(DateTime, server_default=func.now())

    salons = relationship("Salon", back_populates="compte")
    refresh_tokens = relationship("RefreshToken", back_populates="compte")


class Salon(Base):
    __tablename__ = "salons"

    id = Column(Integer, primary_key=True)
    compte_id = Column(Integer, ForeignKey("comptes.id"), nullable=False, index=True)
    nom = Column(String(255), nullable=False)
    slug = Column(String(255), unique=True, nullable=False, index=True)  # identifiant pour l'URL publique /salon/<slug>
    numero_et_rue = Column(String(255))
    complement = Column(String(255))
    code_postal = Column(String(10))
    ville = Column(String(255))
    departement = Column(String(255))
    pays = Column(String(100), nullable=False, default="France")
    latitude = Column(Float)  # calculée automatiquement via Nominatim (voir backend/geocodage.py), jamais saisie par le propriétaire
    longitude = Column(Float)
    description = Column(String(500), nullable=False, default="")
    numero_twilio = Column(String(32), unique=True)  # nullable tant que pas de ligne Twilio
    google_calendar_id = Column(String(255))  # "primary" une fois connecté via OAuth (ou fixe pour le salon de démo)
    google_refresh_token = Column(Text)  # chiffré (Fernet), jamais en clair — voir backend/crypto.py
    calendrier_connecte = Column(Boolean, nullable=False, default=False)
    est_demo = Column(Boolean, nullable=False, default=False)
    horaires = Column(Text, nullable=False)  # JSON : {"lundi": null, "mardi": {"ouverture": "09:00", "fermeture": "19:00"}, ...}
    fermetures_exceptionnelles = Column(Text, nullable=False, default="[]")  # JSON : ["2026-12-25", ...]
    prestations = Column(Text, nullable=False)  # JSON : {"femme": {...}, "homme": {...}, "enfant": {...}}
    photos = Column(Text, nullable=False, default="[]")  # JSON : ["<uuid>.jpg", ...] — fichiers dans uploads/salons/<id>/
    modifie_le = Column(DateTime, server_default=func.now(), onupdate=func.now())

    compte = relationship("Compte", back_populates="salons")
    avis = relationship("Avis", back_populates="salon")


class Avis(Base):
    __tablename__ = "avis"

    id = Column(Integer, primary_key=True)
    salon_id = Column(Integer, ForeignKey("salons.id"), nullable=False, index=True)
    note = Column(Integer, nullable=False)  # 1 à 5
    commentaire = Column(String(500))
    ip_hash = Column(String(64), nullable=False, index=True)  # sha256(ip) — jamais l'IP en clair, voir anti-spam dans backend/main.py
    cree_le = Column(DateTime, server_default=func.now())

    salon = relationship("Salon", back_populates="avis")


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id = Column(Integer, primary_key=True)
    compte_id = Column(Integer, ForeignKey("comptes.id"), nullable=False, index=True)
    token_hash = Column(String(255), nullable=False, unique=True, index=True)  # sha256 du token, jamais le token en clair
    expire_le = Column(DateTime, nullable=False)
    revoque = Column(Boolean, nullable=False, default=False)
    cree_le = Column(DateTime, server_default=func.now())

    compte = relationship("Compte", back_populates="refresh_tokens")
