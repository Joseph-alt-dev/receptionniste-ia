"""Self-check : reproduit le bug du 8 oct 2026 (RefreshError pendant une vraie
conversation, jeton OAuth cassé) — un salon connecté en OAuth dont le jeton
est devenu invalide ne doit JAMAIS basculer silencieusement sur le compte de
service ; l'outil doit répondre poliment, et le salon doit être marqué
"reconnexion Google nécessaire" en base. Utilise une base SQLite temporaire :
ne touche jamais à salons.db ni à Belle Étoile.

Usage : python3 test_refresh_error_reconnexion.py
"""

import asyncio
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import patch

from google.auth.exceptions import RefreshError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import salon_bot
from backend.crypto import chiffrer
from backend.database import Base
from backend.models import Salon
from salon_bot import appeler_outil, construire_client_calendrier, construire_tools

HORAIRES = {jour: {"ouverture": "09:00", "fermeture": "19:00"} for jour in salon_bot.JOURS}
PRESTATIONS = {"femme": {"Coupe": 30.0}}
DATE_FUTURE = (dt.datetime.now(salon_bot.PARIS_TZ).date() + dt.timedelta(days=5)).isoformat()

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base.metadata.create_all(engine)

db_init = TestSession()
db_init.add(Salon(
    id=1, compte_id=1, nom="Salon Test Refresh", slug="salon-test-refresh", description="",
    horaires=json.dumps(HORAIRES), prestations=json.dumps(PRESTATIONS), fermetures_exceptionnelles=json.dumps([]),
    calendrier_connecte=True, google_refresh_token=chiffrer("faux-refresh-token"), google_calendar_id="primary",
    est_demo=True,  # comme Belle Étoile : démo ET connecté en OAuth
))
db_init.commit()
db_init.close()


class ServiceJetonCasse:
    """Simule un appel Google Calendar qui échoue avec le RefreshError exact
    du bug : jeton révoqué/invalide découvert au moment de l'appel réel."""

    def freebusy(self):
        def query(body):
            raise RefreshError(
                "The credentials do not contain the necessary fields need to refresh "
                "the access token. You must specify refresh_token, token_uri, client_id, "
                "and client_secret."
            )
        return SimpleNamespace(query=query)


service_compte_service = SimpleNamespace()  # ne doit jamais être utilisé ici


async def main():
    with (
        patch("salon_bot.SessionLocal", TestSession),
        patch(
            "salon_bot.build_google_service",
            side_effect=lambda *_a, credentials, **_k: (
                ServiceJetonCasse() if isinstance(credentials, salon_bot.GoogleOAuthCredentials) else service_compte_service
            ),
        ),
        patch("salon_bot.charger_salon", side_effect=lambda _id: db_salon()),
    ):
        salon = db_salon()
        service, calendar_id = construire_client_calendrier(salon)
        assert isinstance(service, ServiceJetonCasse)

        outils = construire_tools(salon, service, calendar_id)
        preparer = next(f for f in outils if f.__name__ == "preparer_reservation")

        resultat = await appeler_outil(preparer, {
            "prestation": "Coupe", "date": DATE_FUTURE, "heure": "10:00", "nom_client": "Joseph",
        })

        assert resultat["succes"] is False, resultat
        assert "erreur technique" in resultat["message"].lower(), resultat
        assert "RefreshError" not in resultat["message"] and "refresh_token" not in resultat["message"], (
            "jamais de détail technique ou de jeton dans le message renvoyé au client"
        )

        db_verif = TestSession()
        salon_relu = db_verif.get(Salon, 1)
        assert salon_relu.google_reconnexion_necessaire is True, (
            "le salon doit être marqué reconnexion Google nécessaire après un RefreshError"
        )
        db_verif.close()

    print(
        "OK — un jeton OAuth cassé (RefreshError) pendant preparer_reservation ne plante pas la conversation, "
        "ne bascule jamais sur le compte de service, et marque bien le salon reconnexion Google nécessaire."
    )


def db_salon() -> Salon:
    db = TestSession()
    try:
        return db.get(Salon, 1)
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
