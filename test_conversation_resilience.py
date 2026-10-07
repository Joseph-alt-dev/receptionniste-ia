"""Self-check pour la résilience de ConversationTexte face aux erreurs de l'API
LLM (voir bug du 7 oct 2026 : un 400 tool_use_failed de Groq/gpt-oss faisait
planter toute la conversation) et pour le nettoyage du Markdown côté serveur.
N'appelle aucune vraie API : le client OpenAI est patché.

Usage : python3 test_conversation_resilience.py
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from openai import APIConnectionError, BadRequestError, RateLimitError

import salon_bot
from salon_bot import MESSAGE_ERREUR_LLM, _nettoyer_markdown

FAUSSE_REQUETE = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
FAUSSE_REPONSE_400 = httpx.Response(400, request=FAUSSE_REQUETE)


def erreur_tool_use_failed():
    return BadRequestError("tool_use_failed", response=FAUSSE_REPONSE_400, body=None)


def message_texte(contenu):
    return SimpleNamespace(
        model_dump=lambda exclude_none=True: {"role": "assistant", "content": contenu},
        tool_calls=None,
        content=contenu,
    )


def faux_salon():
    return SimpleNamespace(
        id=1, nom="Salon Test", numero_et_rue=None, complement=None, code_postal=None, ville=None, pays="France",
        prestations=json.dumps({"femme": {"Coupe": 30.0}}),
        horaires=json.dumps({j: {"ouverture": "09:00", "fermeture": "19:00"} for j in salon_bot.JOURS}),
        fermetures_exceptionnelles=json.dumps([]),
        calendrier_connecte=False, google_refresh_token=None, est_demo=False,
    )


async def main():
    with (
        patch("salon_bot.construire_client_calendrier", return_value=(None, None)),
        patch("salon_bot.charger_salon", return_value=faux_salon()),
        patch.dict("os.environ", {"GROQ_API_KEY": "fausse-cle"}),
    ):
        conv = salon_bot.ConversationTexte(faux_salon(), canal="chat")

        # 1) tool_use_failed persistant -> épuise les relances, répond poliment, ne lève rien
        conv._client.chat.completions.create = AsyncMock(side_effect=erreur_tool_use_failed())
        reponse = await conv.tour("Bonjour")
        assert reponse == MESSAGE_ERREUR_LLM, reponse
        assert conv._client.chat.completions.create.await_count == salon_bot.MAX_RELANCES_TOOL_USE_FAILED + 1

        # 2) tool_use_failed une seule fois puis succès -> la relance suffit, pas d'erreur visible
        conv._client.chat.completions.create = AsyncMock(
            side_effect=[erreur_tool_use_failed(), SimpleNamespace(choices=[SimpleNamespace(message=message_texte("Bonjour, je suis Rachel."))])]
        )
        reponse = await conv.tour("Bonjour")
        assert reponse == "Bonjour, je suis Rachel."

        # 3) rate limit -> abandon immédiat (pas de relance), réponse polie
        conv._client.chat.completions.create = AsyncMock(
            side_effect=RateLimitError("rate limit", response=FAUSSE_REPONSE_400, body=None)
        )
        reponse = await conv.tour("Bonjour")
        assert reponse == MESSAGE_ERREUR_LLM
        assert conv._client.chat.completions.create.await_count == 1

        # 4) erreur de connexion -> idem, abandon propre
        conv._client.chat.completions.create = AsyncMock(
            side_effect=APIConnectionError(request=FAUSSE_REQUETE)
        )
        reponse = await conv.tour("Bonjour")
        assert reponse == MESSAGE_ERREUR_LLM

        # 5) markdown : même si le LLM renvoie des astérisques, le serveur les retire
        conv._client.chat.completions.create = AsyncMock(
            return_value=SimpleNamespace(choices=[SimpleNamespace(message=message_texte("**jeudi 8 octobre 2026 à 15h00**"))])
        )
        reponse = await conv.tour("Confirme")
        assert reponse == "jeudi 8 octobre 2026 à 15h00", reponse

    assert _nettoyer_markdown("**gras** et texte") == "gras et texte"

    print("OK — ConversationTexte survit à tool_use_failed/rate limit/connexion sans planter, et retire le Markdown.")


if __name__ == "__main__":
    asyncio.run(main())
