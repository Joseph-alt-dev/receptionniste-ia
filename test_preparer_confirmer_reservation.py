"""Self-check pour le flux de réservation en deux temps preparer_reservation /
confirmer_reservation (voir bugs du 7 oct 2026 : prestations jamais relues
depuis la base, réservation créée sans prestation). N'utilise aucune vraie
base de données ni vrai salon : charger_salon est patché pour renvoyer un faux
salon (dont le contenu peut être muté entre deux appels, pour simuler une
modification faite dans le dashboard), et le service Google Calendar est un
faux objet en mémoire.

Usage : python3 test_preparer_confirmer_reservation.py
"""

import asyncio
import datetime as dt
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import salon_bot
from salon_bot import appeler_outil, construire_tools

AUJOURDHUI = dt.datetime.now(salon_bot.PARIS_TZ).date()
DATE_FUTURE = (AUJOURDHUI + dt.timedelta(days=5)).isoformat()
JOUR_SEMAINE = salon_bot.JOURS[(AUJOURDHUI + dt.timedelta(days=5)).weekday()]
HORAIRES = {jour: {"ouverture": "09:00", "fermeture": "19:00"} for jour in salon_bot.JOURS}
PRESTATIONS = {"femme": {"Coupe": 30.0}}

etat_salon = {"prestations": PRESTATIONS}


def faux_salon_actuel() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        prestations=json.dumps(etat_salon["prestations"]),
        horaires=json.dumps(HORAIRES),
        fermetures_exceptionnelles=json.dumps([]),
    )


class FauxRequeteExecutable:
    def __init__(self, resultat):
        self._resultat = resultat

    def execute(self):
        return self._resultat


class FauxService:
    """Simule juste assez de l'API Google Calendar pour ces tests."""

    def __init__(self):
        self.occupe = False
        self.evenements_crees = []

    def freebusy(self):
        busy = [{"start": "x", "end": "y"}] if self.occupe else []
        return SimpleNamespace(query=lambda body: FauxRequeteExecutable({"calendars": {"primary": {"busy": busy}}}))

    def events(self):
        def insert(calendarId, body):
            evt = {"id": f"evt-{len(self.evenements_crees)}", "start": body["start"]}
            self.evenements_crees.append(evt)
            return FauxRequeteExecutable(evt)

        def get(calendarId, eventId):
            return FauxRequeteExecutable(next(e for e in self.evenements_crees if e["id"] == eventId))

        return SimpleNamespace(insert=insert, get=get)


async def main():
    with patch("salon_bot.charger_salon", side_effect=lambda _id: faux_salon_actuel()):
        service = FauxService()
        outils = construire_tools(SimpleNamespace(id=1), service, "primary")
        preparer = next(f for f in outils if f.__name__ == "preparer_reservation")
        confirmer = next(f for f in outils if f.__name__ == "confirmer_reservation")

        # 1) prestation inconnue -> refusé, pas de création
        r = await appeler_outil(preparer, {
            "prestation": "Taper", "date": DATE_FUTURE, "heure": "10:00", "nom_client": "Joseph",
        })
        assert r["succes"] is False and "ne trouve pas de prestation" in r["message"]

        # 2) la prestation "Taper" est ajoutée dans le dashboard (mutation de l'état),
        #    sans redémarrer quoi que ce soit : doit être acceptée au prochain appel.
        etat_salon["prestations"] = {"femme": {"Coupe": 30.0}, "homme": {"Taper": 20.0}}
        r = await appeler_outil(preparer, {
            "prestation": "taper", "date": DATE_FUTURE, "heure": "10:00", "nom_client": "Joseph",
        })
        assert r["succes"] is True, r
        assert r["recapitulatif"] == (
            f"Je récapitule : Taper, {JOUR_SEMAINE} {(AUJOURDHUI + dt.timedelta(days=5)).day} "
            f"{salon_bot.MOIS[(AUJOURDHUI + dt.timedelta(days=5)).month - 1]} "
            f"{(AUJOURDHUI + dt.timedelta(days=5)).year} à 10:00, au nom de Joseph. Je confirme la réservation ?"
        )
        id_attente = r["id_attente"]
        assert not service.evenements_crees, "preparer_reservation ne doit RIEN créer dans l'agenda"

        # 3) créneau occupé -> refusé
        service.occupe = True
        r2 = await appeler_outil(preparer, {
            "prestation": "Taper", "date": DATE_FUTURE, "heure": "11:00", "nom_client": "Joseph",
        })
        assert r2["succes"] is False and "déjà occupé" in r2["message"]
        service.occupe = False

        # 4) confirmer_reservation sans préparation préalable -> refusé
        r3 = await appeler_outil(confirmer, {"id_attente": "inconnu"})
        assert r3["succes"] is False and "Aucune réservation en attente" in r3["message"]

        # 5) id_attente expiré -> refusé, forcé à revalider
        outils2 = construire_tools(SimpleNamespace(id=1), service, "primary")
        preparer2 = next(f for f in outils2 if f.__name__ == "preparer_reservation")
        confirmer2 = next(f for f in outils2 if f.__name__ == "confirmer_reservation")
        r4 = await appeler_outil(preparer2, {
            "prestation": "Taper", "date": DATE_FUTURE, "heure": "12:00", "nom_client": "Marie",
        })
        id_attente_a_expirer = r4["id_attente"]
        with patch("salon_bot.time.time", return_value=time.time() + salon_bot.DUREE_RESERVATION_EN_ATTENTE_SECONDES + 1):
            r5 = await appeler_outil(confirmer2, {"id_attente": id_attente_a_expirer})
        assert r5["succes"] is False and "expiré" in r5["message"]

        # 6) flux complet : préparer puis confirmer -> crée réellement l'événement
        r6 = await appeler_outil(confirmer, {"id_attente": id_attente})
        assert r6["succes"] is True, r6
        assert r6["message"] == (
            f"C'est réservé : Taper, {JOUR_SEMAINE} {(AUJOURDHUI + dt.timedelta(days=5)).day} "
            f"{salon_bot.MOIS[(AUJOURDHUI + dt.timedelta(days=5)).month - 1]} "
            f"{(AUJOURDHUI + dt.timedelta(days=5)).year} à 10:00, au nom de Joseph. Puis-je vous aider pour autre chose ?"
        )
        assert len(service.evenements_crees) == 1, "un seul événement doit avoir été créé au total"

        # 7) ce même id_attente ne peut pas être réutilisé (consommé au premier succès)
        r7 = await appeler_outil(confirmer, {"id_attente": id_attente})
        assert r7["succes"] is False and "Aucune réservation en attente" in r7["message"]

    print("OK — preparer_reservation/confirmer_reservation : prestations fraîches, aucune création sans prestation valide, expiration et non-réutilisation respectées.")


if __name__ == "__main__":
    asyncio.run(main())
