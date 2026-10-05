"""Self-check pour la résolution de dates de salon_bot.py (voir pipecat/chat
bugs du 5 oct 2026 : le modèle ne doit jamais calculer un jour de semaine ou
une date relative lui-même, ces fonctions le font côté serveur).

Usage : python3 test_salon_bot_dates.py
"""

import datetime as dt

from salon_bot import _prochain_jour_semaine, _prochaine_occurrence, _raison_fermeture

AUJOURDHUI = dt.date(2026, 10, 5)  # un lundi

# _prochaine_occurrence : année omise -> prochaine occurrence à venir
assert _prochaine_occurrence(1, 10, None, AUJOURDHUI) == (dt.date(2027, 10, 1), True)  # 1er oct déjà passé -> l'an prochain
assert _prochaine_occurrence(20, 10, None, AUJOURDHUI) == (dt.date(2026, 10, 20), True)  # pas encore passé -> cette année
assert _prochaine_occurrence(5, 10, None, AUJOURDHUI) == (dt.date(2026, 10, 5), True)  # aujourd'hui même -> pas "passé"

# _prochaine_occurrence : année précisée -> utilisée telle quelle (même si passée)
assert _prochaine_occurrence(1, 10, 2020, AUJOURDHUI) == (dt.date(2020, 10, 1), False)

# _prochain_jour_semaine : aujourd'hui compte s'il correspond, sinon prochaine occurrence
assert _prochain_jour_semaine("lundi", AUJOURDHUI) == AUJOURDHUI
assert _prochain_jour_semaine("mardi", AUJOURDHUI) == dt.date(2026, 10, 6)
assert _prochain_jour_semaine("dimanche", AUJOURDHUI) == dt.date(2026, 10, 11)

# _raison_fermeture : jour fermé par horaires, fermeture exceptionnelle, jour ouvert
horaires = {"lundi": None, "mardi": {"ouverture": "09:00", "fermeture": "19:00"}}
fermetures = {"2026-10-06"}
assert _raison_fermeture(AUJOURDHUI, horaires, fermetures) == "le salon est fermé le lundi"
assert _raison_fermeture(dt.date(2026, 10, 6), horaires, fermetures) == "fermeture exceptionnelle ce jour-là"
assert _raison_fermeture(dt.date(2026, 10, 13), horaires, fermetures) is None

print("OK — toutes les assertions ont passé.")
