"""Simule un appel texte pour un salon donné, sans Twilio ni audio.

Usage :
    python simuler_appel.py                  # salon de démo
    python simuler_appel.py --salon-id 2      # un salon précis

Charge les vraies données du salon (horaires, prestations, calendrier) via
salon_bot.py et fait tourner la même logique (prompt, outils) dans une boucle
texte au terminal, pour vérifier que tout charge correctement avant de
brancher Twilio.
"""

import argparse
import asyncio
import sys

from salon_bot import ConversationTexte, charger_salon, charger_salon_demo, construire_client_calendrier


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--salon-id", type=int, default=None, help="Id du salon à tester (défaut : salon de démo)")
    args = parser.parse_args()

    salon = charger_salon(args.salon_id) if args.salon_id else charger_salon_demo()
    if salon is None:
        sys.exit(f"Salon {args.salon_id} introuvable.")

    service, _ = construire_client_calendrier(salon)
    statut_calendrier = (
        "compte de service (démo)" if salon.est_demo
        else "connecté via OAuth" if service
        else "non connecté — repli activé"
    )
    print(f"--- Simulation d'appel pour « {salon.nom} » (id={salon.id}) — calendrier : {statut_calendrier} ---\n")

    def tracer_appel_outil(nom, arguments, resultat):
        print(f"[outil] {nom}({arguments})")
        print(f"[résultat] {resultat}")

    conversation = ConversationTexte(salon)

    async def tour(message_utilisateur):
        reponse = await conversation.tour(message_utilisateur, on_appel_outil=tracer_appel_outil)
        print(f"Claire: {reponse}\n")

    await tour("[Le client vient de décrocher. Présente-toi brièvement.]")
    while True:
        try:
            texte = input("Vous: ")
        except EOFError:
            break
        if texte.strip().lower() in {"quit", "exit", ""}:
            break
        await tour(texte)


if __name__ == "__main__":
    asyncio.run(main())
