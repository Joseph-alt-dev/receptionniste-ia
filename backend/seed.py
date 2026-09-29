"""Crée les tables et pré-remplit le salon de démo "Belle Étoile", utilisé
par bot.py en mode webrtc quand aucun numéro Twilio n'est composé.
"""

import json

from backend.database import Base, SessionLocal, engine
from backend.models import Compte, Salon
from backend.security import hash_password

HORAIRES_BELLE_ETOILE = {
    "lundi": None,
    "mardi": {"ouverture": "09:00", "fermeture": "19:00"},
    "mercredi": {"ouverture": "09:00", "fermeture": "19:00"},
    "jeudi": {"ouverture": "09:00", "fermeture": "19:00"},
    "vendredi": {"ouverture": "09:00", "fermeture": "19:00"},
    "samedi": {"ouverture": "09:00", "fermeture": "19:00"},
    "dimanche": None,
}

PRESTATIONS_BELLE_ETOILE = {
    "femme": {
        "coupe_cheveux_courts": 35,
        "coupe_cheveux_mi_longs": 40,
        "coupe_cheveux_longs": 45,
        "brushing_court": 20,
        "brushing_mi_long": 25,
        "brushing_long": 30,
        "couleur_racines": 45,
        "couleur_complete": 60,
        "meches_balayage": 80,
        "soin_keratine": 50,
        "chignon_coiffure_evenementielle": 55,
    },
    "homme": {
        "coupe_classique": 25,
        "coupe_degradee": 28,
        "coupe_et_barbe": 35,
        "taille_barbe_seule": 15,
        "coloration_homme": 30,
    },
    "enfant": {
        "coupe_moins_10_ans": 18,
        "coupe_10_a_14_ans": 22,
    },
}


def seed():
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        if db.query(Salon).filter_by(est_demo=True).first():
            print("Salon de démo déjà présent, rien à faire.")
            return

        compte_demo = Compte(
            email="demo@belleetoile.fr",
            mot_de_passe_hash=hash_password("changez-moi"),
        )
        db.add(compte_demo)
        db.flush()  # pour obtenir compte_demo.id avant de créer le salon

        salon_demo = Salon(
            compte_id=compte_demo.id,
            nom="Belle Étoile",
            adresse="15e arrondissement, Paris",
            google_calendar_id="joseph.quesne@ensae.fr",
            est_demo=True,
            horaires=json.dumps(HORAIRES_BELLE_ETOILE),
            prestations=json.dumps(PRESTATIONS_BELLE_ETOILE),
        )
        db.add(salon_demo)
        db.commit()
        print(f"Salon de démo créé (compte {compte_demo.email} / mot de passe : changez-moi)")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
