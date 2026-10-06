import re
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    mot_de_passe: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    mot_de_passe: str


class PlageHoraire(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ouverture: str
    fermeture: str

    @field_validator("ouverture", "fermeture")
    @classmethod
    def format_heure(cls, v: str) -> str:
        try:
            h, m = v.split(":")
            assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
        except Exception:
            raise ValueError("format attendu HH:MM")
        return v


class Horaires(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lundi: PlageHoraire | None = None
    mardi: PlageHoraire | None = None
    mercredi: PlageHoraire | None = None
    jeudi: PlageHoraire | None = None
    vendredi: PlageHoraire | None = None
    samedi: PlageHoraire | None = None
    dimanche: PlageHoraire | None = None


class Prestations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    femme: dict[str, float] = Field(default_factory=dict)
    homme: dict[str, float] = Field(default_factory=dict)
    enfant: dict[str, float] = Field(default_factory=dict)

    @field_validator("femme", "homme", "enfant")
    @classmethod
    def prix_positifs(cls, v: dict[str, float]) -> dict[str, float]:
        for nom, prix in v.items():
            if prix <= 0:
                raise ValueError(f"prix invalide pour {nom!r} : doit être positif")
        return v


class SalonBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nom: str = Field(min_length=1, max_length=255)
    numero_et_rue: str | None = Field(default=None, max_length=255)
    complement: str | None = Field(default=None, max_length=255)
    code_postal: str | None = Field(default=None, max_length=10)
    ville: str | None = Field(default=None, max_length=255)
    departement: str | None = Field(default=None, max_length=255)
    pays: str = Field(default="France", max_length=100)
    description: str = Field(default="", max_length=500)
    horaires: Horaires
    fermetures_exceptionnelles: list[date] = Field(default_factory=list)
    prestations: Prestations

    @model_validator(mode="after")
    def valider_code_postal(self):
        if self.pays.strip().lower() == "france" and self.code_postal and not re.fullmatch(r"\d{5}", self.code_postal.strip()):
            raise ValueError("Code postal invalide : 5 chiffres attendus pour la France")
        return self


class SalonCreate(SalonBase):
    pass


class SalonUpdate(SalonBase):
    pass


class SalonOut(SalonBase):
    id: int
    slug: str
    numero_twilio: str | None = None
    est_demo: bool
    calendrier_connecte: bool
    google_compte_email: str | None = None
    photos: list[str] = Field(default_factory=list)
    adresse_complete: str = ""
    latitude: float | None = None
    longitude: float | None = None
    note_moyenne: float | None = None
    nombre_avis: int = 0


class AvisCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: int = Field(ge=1, le=5)
    commentaire: str | None = Field(default=None, max_length=500)


class AvisOut(BaseModel):
    note: int
    commentaire: str | None = None
    cree_le: datetime


class SalonRechercheResultat(BaseModel):
    nom: str
    slug: str
    ville: str | None = None
    note_moyenne: float | None = None
    nombre_avis: int = 0
    photo_principale: str | None = None
    distance_km: float | None = None


class SalonResume(BaseModel):
    id: int
    nom: str
    est_demo: bool


class CompteAdminOut(BaseModel):
    id: int
    email: EmailStr
    actif: bool
    est_admin: bool
    salons: list[SalonResume]
