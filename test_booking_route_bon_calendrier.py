"""Self-check : un salon connecté en OAuth et marqué est_demo=True (cas de Belle
Étoile) doit réserver dans SON agenda OAuth, jamais dans celui du compte de
service, même via le flux preparer_reservation/confirmer_reservation (régression
suspectée le 8 oct 2026 après l'introduction de ce flux dans 0329b3a).

Usage : python3 test_booking_route_bon_calendrier.py
"""

import asyncio
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import patch

import salon_bot
from backend.crypto import chiffrer
from salon_bot import appeler_outil, construire_client_calendrier, construire_tools

HORAIRES = {jour: {"ouverture": "09:00", "fermeture": "19:00"} for jour in salon_bot.JOURS}
PRESTATIONS = {"femme": {"Coupe": 30.0}}
AUJOURDHUI = dt.datetime.now(salon_bot.PARIS_TZ).date()
DATE_FUTURE = (AUJOURDHUI + dt.timedelta(days=5)).isoformat()


class FauxRequeteExecutable:
    def __init__(self, resultat):
        self._resultat = resultat

    def execute(self):
        return self._resultat


class FauxService:
    """Agenda factice : chaque instance représente un compte Google distinct,
    pour vérifier dans lequel des deux l'événement est réellement créé."""

    def __init__(self, nom):
        self.nom = nom
        self.evenements_crees = []

    def freebusy(self):
        return SimpleNamespace(query=lambda body: FauxRequeteExecutable({"calendars": {"primary": {"busy": []}}}))

    def events(self):
        def insert(calendarId, body):
            evt = {"id": f"evt-{self.nom}-{len(self.evenements_crees)}", "start": body["start"]}
            self.evenements_crees.append(evt)
            return FauxRequeteExecutable(evt)

        def get(calendarId, eventId):
            return FauxRequeteExecutable(next(e for e in self.evenements_crees if e["id"] == eventId))

        return SimpleNamespace(insert=insert, get=get)


def faux_salon_belle_etoile():
    return SimpleNamespace(
        id=1,
        prestations=json.dumps(PRESTATIONS),
        horaires=json.dumps(HORAIRES),
        fermetures_exceptionnelles=json.dumps([]),
        calendrier_connecte=True,
        google_refresh_token=chiffrer("faux-refresh-token"),
        google_calendar_id="primary",
        est_demo=True,  # comme Belle Étoile : démo ET connecté en OAuth
    )


service_oauth = FauxService("oauth")
service_compte_service = FauxService("compte_service")


async def main():
    with (
        patch(
            "salon_bot.build_google_service",
            side_effect=lambda *_a, credentials, **_k: (
                service_oauth if isinstance(credentials, salon_bot.GoogleOAuthCredentials) else service_compte_service
            ),
        ),
        patch.object(salon_bot.GoogleServiceCredentials, "from_service_account_file", return_value="ignore"),
        patch("salon_bot.charger_salon", side_effect=lambda _id: faux_salon_belle_etoile()),
    ):
        salon = faux_salon_belle_etoile()
        service, calendar_id = construire_client_calendrier(salon)
        assert service is service_oauth, "Belle Étoile (est_demo=True + OAuth connecté) doit utiliser son agenda OAuth"

        outils = construire_tools(salon, service, calendar_id)
        preparer = next(f for f in outils if f.__name__ == "preparer_reservation")
        confirmer = next(f for f in outils if f.__name__ == "confirmer_reservation")

        r = await appeler_outil(preparer, {
            "prestation": "Coupe", "date": DATE_FUTURE, "heure": "10:00", "nom_client": "Test",
        })
        assert r["succes"] is True, r
        r2 = await appeler_outil(confirmer, {"id_attente": r["id_attente"]})
        assert r2["succes"] is True, r2

        assert len(service_oauth.evenements_crees) == 1, "l'événement doit être créé dans l'agenda OAuth"
        assert len(service_compte_service.evenements_crees) == 0, (
            "RÉGRESSION : la réservation a été créée dans l'agenda du compte de service "
            "au lieu de l'agenda OAuth du propriétaire"
        )

    print(
        "OK — preparer_reservation/confirmer_reservation réservent bien dans l'agenda OAuth, "
        "jamais dans celui du compte de service, pour un salon est_demo=True connecté en OAuth."
    )


if __name__ == "__main__":
    asyncio.run(main())
