from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import relationship

from backend.database import Base


class Compte(Base):
    __tablename__ = "comptes"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    mot_de_passe_hash = Column(String(255), nullable=False)
    cree_le = Column(DateTime, server_default=func.now())

    salons = relationship("Salon", back_populates="compte")
    refresh_tokens = relationship("RefreshToken", back_populates="compte")


class Salon(Base):
    __tablename__ = "salons"

    id = Column(Integer, primary_key=True)
    compte_id = Column(Integer, ForeignKey("comptes.id"), nullable=False, index=True)
    nom = Column(String(255), nullable=False)
    adresse = Column(String(255))
    numero_twilio = Column(String(32), unique=True)  # nullable tant que pas de ligne Twilio
    google_calendar_id = Column(String(255), nullable=False)
    est_demo = Column(Boolean, nullable=False, default=False)
    horaires = Column(Text, nullable=False)  # JSON : {"lundi": null, "mardi": {"ouverture": "09:00", "fermeture": "19:00"}, ...}
    fermetures_exceptionnelles = Column(Text, nullable=False, default="[]")  # JSON : ["2026-12-25", ...]
    prestations = Column(Text, nullable=False)  # JSON : {"femme": {...}, "homme": {...}, "enfant": {...}}
    modifie_le = Column(DateTime, server_default=func.now(), onupdate=func.now())

    compte = relationship("Compte", back_populates="salons")


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id = Column(Integer, primary_key=True)
    compte_id = Column(Integer, ForeignKey("comptes.id"), nullable=False, index=True)
    token_hash = Column(String(255), nullable=False, unique=True, index=True)  # sha256 du token, jamais le token en clair
    expire_le = Column(DateTime, nullable=False)
    revoque = Column(Boolean, nullable=False, default=False)
    cree_le = Column(DateTime, server_default=func.now())

    compte = relationship("Compte", back_populates="refresh_tokens")
