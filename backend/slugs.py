import re
import unicodedata

from sqlalchemy.orm import Session

from backend.models import Salon


def generer_slug(nom: str, db: Session) -> str:
    """Construit un slug URL-friendly à partir du nom du salon, en garantissant
    son unicité en base (ajoute -2, -3, ... en cas de collision).
    """
    base = unicodedata.normalize("NFKD", nom).encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "salon"

    slug = base
    compteur = 2
    while db.query(Salon).filter_by(slug=slug).first():
        slug = f"{base}-{compteur}"
        compteur += 1
    return slug
