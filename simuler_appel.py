"""Simule un appel texte pour un salon donné, sans Twilio ni audio.

Usage :
    python simuler_appel.py                  # salon de démo
    python simuler_appel.py --salon-id 2      # un salon précis

Charge les vraies données du salon (horaires, prestations, calendrier) via
bot.py et fait tourner la même logique (prompt, outils) dans une boucle
texte au terminal, pour vérifier que tout charge correctement avant de
brancher Twilio.
"""

import argparse
import asyncio
import json
import os
import sys

from openai import OpenAI
from pipecat.adapters.schemas.direct_function import DirectFunctionWrapper
from pipecat.services.llm_service import FunctionCallParams

from bot import (
    charger_salon,
    charger_salon_demo,
    construire_client_calendrier,
    construire_system_prompt,
    construire_tools,
)


async def appeler_outil(fonction, arguments: dict) -> dict:
    resultat = {}

    async def callback(valeur, **_kwargs):
        resultat.update(valeur)

    params = FunctionCallParams(
        function_name=fonction.__name__,
        tool_call_id="local-test",
        arguments=arguments,
        llm=None,
        pipeline_worker=None,
        context=None,
        result_callback=callback,
    )
    await fonction(params, **arguments)
    return resultat


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--salon-id", type=int, default=None, help="Id du salon à tester (défaut : salon de démo)")
    args = parser.parse_args()

    salon = charger_salon(args.salon_id) if args.salon_id else charger_salon_demo()
    if salon is None:
        sys.exit(f"Salon {args.salon_id} introuvable.")

    service, calendar_id = construire_client_calendrier(salon)
    statut_calendrier = (
        "compte de service (démo)" if salon.est_demo
        else "connecté via OAuth" if service
        else "non connecté — repli activé"
    )
    print(f"--- Simulation d'appel pour « {salon.nom} » (id={salon.id}) — calendrier : {statut_calendrier} ---\n")

    outils = construire_tools(salon, service, calendar_id)
    outils_par_nom = {f.__name__: f for f in outils}
    outils_openai = [
        {"type": "function", "function": DirectFunctionWrapper(f).to_function_schema().to_default_dict()}
        for f in outils
    ]

    client = OpenAI(api_key=os.environ["GROQ_API_KEY"], base_url="https://api.groq.com/openai/v1")
    messages = [{"role": "system", "content": construire_system_prompt(salon)}]

    def tour(message_utilisateur: str | None):
        if message_utilisateur is not None:
            messages.append({"role": "user", "content": message_utilisateur})
        while True:
            reponse = client.chat.completions.create(
                model="openai/gpt-oss-20b", messages=messages, tools=outils_openai,
            )
            message = reponse.choices[0].message
            messages.append(message.model_dump(exclude_none=True))
            if not message.tool_calls:
                print(f"Claire: {message.content}\n")
                return
            for appel in message.tool_calls:
                arguments = json.loads(appel.function.arguments or "{}")
                print(f"[outil] {appel.function.name}({arguments})")
                resultat = asyncio.run(appeler_outil(outils_par_nom[appel.function.name], arguments))
                print(f"[résultat] {resultat}")
                messages.append({"role": "tool", "tool_call_id": appel.id, "content": json.dumps(resultat)})

    tour("[Le client vient de décrocher. Présente-toi brièvement.]")
    while True:
        try:
            texte = input("Vous: ")
        except EOFError:
            break
        if texte.strip().lower() in {"quit", "exit", ""}:
            break
        tour(texte)


if __name__ == "__main__":
    main()
