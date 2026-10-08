"""Self-check : le WebSocket /chat/ws envoie un message structuré
`reservation_confirmee` uniquement quand `confirmer_reservation` a réellement
réussi (jamais basé sur le texte du LLM), et ne l'envoie jamais en cas de
refus. Utilise une base SQLite temporaire et un salon jetable : ne touche
jamais à salons.db ni à Belle Étoile.

Usage : python3 test_reservation_confirmee_ws.py
"""

import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import backend.main as main
from backend.database import Base
from backend.models import Salon

engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base.metadata.create_all(engine)


def get_db_test():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


main.app.dependency_overrides[main.get_db] = get_db_test

db_init = TestSession()
db_init.add(Salon(
    id=1,
    compte_id=1,
    nom="Salon Test Modale",
    slug="salon-test-modale",
    description="",
    horaires=json.dumps({}),
    prestations=json.dumps({}),
    fermetures_exceptionnelles=json.dumps([]),
))
db_init.commit()
db_init.close()


class FauxConversation:
    """Remplace ConversationTexte : simule un tour de conversation sans appeler
    de vrai LLM, pour piloter précisément le scénario testé (succès/refus)."""

    comportement = "reussite"  # modifié entre les scénarios

    def __init__(self, salon, canal):
        self.salon = salon

    async def tour(self, texte_utilisateur, on_appel_outil=None):
        if texte_utilisateur.startswith("[Le client"):
            return "Bonjour, comment puis-je vous aider ?"
        if FauxConversation.comportement == "reussite":
            if on_appel_outil:
                on_appel_outil("confirmer_reservation", {}, {
                    "succes": True,
                    "message": "C'est réservé.",
                    "id_evenement": "evt-test-1",
                    "prestation": "Coupe",
                    "date": "2026-10-10",
                    "date_affichage": "samedi 10 octobre 2026",
                    "heure": "10:00",
                    "nom_client": "Alice",
                })
            return "C'est réservé : Coupe, samedi 10 octobre 2026 à 10:00, au nom d'Alice."
        # refus : confirmer_reservation échoue, on_appel_outil reçoit succes=False
        if on_appel_outil:
            on_appel_outil("confirmer_reservation", {}, {
                "succes": False,
                "message": "Ce créneau n'est plus disponible.",
            })
        return "Désolée, ce créneau n'est plus disponible."


main.ConversationTexte = FauxConversation

client = TestClient(main.app)


def _ouvrir_et_ignorer_accueil(ws):
    ws.receive_json()  # {"role": "info", ...}
    ws.receive_json()  # réponse au message de bienvenue automatique


def test_reservation_reussie_envoie_la_confirmation():
    FauxConversation.comportement = "reussite"
    with client.websocket_connect("/chat/ws?salon_id=1") as ws:
        _ouvrir_et_ignorer_accueil(ws)
        ws.send_json({"message": "Je veux une coupe samedi à 10h, je m'appelle Alice"})
        reponse_texte = ws.receive_json()
        assert reponse_texte["role"] == "assistant"
        confirmation = ws.receive_json()
        assert confirmation["type"] == "reservation_confirmee"
        assert confirmation["salon"] == "Salon Test Modale"
        assert confirmation["prestation"] == "Coupe"
        assert confirmation["date_affichage"] == "samedi 10 octobre 2026"
        assert confirmation["heure"] == "10:00"
        assert confirmation["prenom"] == "Alice"
    print("OK — une réservation réussie déclenche bien le message reservation_confirmee, avec les bonnes infos.")


def test_creneau_refuse_n_envoie_rien():
    # Le websocket attend un message client avant d'en émettre un autre : en
    # enchaînant un tour "refus" puis un tour "réussite" et en lisant les
    # messages dans l'ordre strict, on prouve qu'aucun reservation_confirmee
    # n'a été glissé après le refus, sans jamais bloquer sur une réception
    # qui n'arriverait pas.
    FauxConversation.comportement = "refus"
    with client.websocket_connect("/chat/ws?salon_id=1") as ws:
        _ouvrir_et_ignorer_accueil(ws)
        ws.send_json({"message": "Je veux une coupe hier à 10h"})
        reponse_refus = ws.receive_json()
        assert reponse_refus["role"] == "assistant"

        FauxConversation.comportement = "reussite"
        ws.send_json({"message": "Je veux une coupe samedi à 10h, je m'appelle Alice"})
        reponse_texte = ws.receive_json()
        assert reponse_texte["role"] == "assistant"
        confirmation = ws.receive_json()
        assert confirmation["type"] == "reservation_confirmee"
    print("OK — un créneau refusé ne déclenche jamais reservation_confirmee (le tour suivant, lui, le déclenche bien).")


if __name__ == "__main__":
    test_reservation_reussie_envoie_la_confirmation()
    test_creneau_refuse_n_envoie_rien()
