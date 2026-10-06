"""Self-check pour la résolution de dates de salon_bot.py (voir bug du 6 oct
2026 : une date jour+mois sans année déjà passée cette année ne doit JAMAIS
basculer silencieusement sur l'année suivante, et aucune réservation à plus
de 90 jours ne doit être acceptée).

Usage : python3 test_salon_bot_dates.py
"""

import datetime as dt

from salon_bot import (
    LIMITE_JOURS_RESERVATION,
    _prochain_jour_semaine,
    _raison_fermeture,
    _resoudre_jour_mois,
)

AUJOURDHUI = dt.date(2026, 10, 6)  # un mardi

# _resoudre_jour_mois : année omise -> année en cours, MÊME SI la date est déjà
# passée cette année (pas de bascule silencieuse sur l'année prochaine).
assert _resoudre_jour_mois(1, 10, None, AUJOURDHUI) == dt.date(2026, 10, 1)  # 1er oct : déjà passé, reste cette année
assert _resoudre_jour_mois(20, 10, None, AUJOURDHUI) == dt.date(2026, 10, 20)  # pas encore passé -> cette année
assert _resoudre_jour_mois(6, 10, None, AUJOURDHUI) == dt.date(2026, 10, 6)  # aujourd'hui même

# _resoudre_jour_mois : année précisée -> utilisée telle quelle (même passée ou lointaine)
assert _resoudre_jour_mois(1, 10, 2020, AUJOURDHUI) == dt.date(2020, 10, 1)
assert _resoudre_jour_mois(1, 10, 2027, AUJOURDHUI) == dt.date(2027, 10, 1)

# _prochain_jour_semaine : aujourd'hui compte s'il correspond, sinon prochaine occurrence
assert _prochain_jour_semaine("mardi", AUJOURDHUI) == AUJOURDHUI
assert _prochain_jour_semaine("samedi", AUJOURDHUI) == dt.date(2026, 10, 10)
assert _prochain_jour_semaine("lundi", AUJOURDHUI) == dt.date(2026, 10, 12)

# _raison_fermeture : jour fermé par horaires, fermeture exceptionnelle, jour ouvert
horaires = {"lundi": None, "mardi": {"ouverture": "09:00", "fermeture": "19:00"}}
fermetures = {"2026-10-20"}
assert _raison_fermeture(AUJOURDHUI, horaires, fermetures) is None  # mardi ouvert
assert _raison_fermeture(dt.date(2026, 10, 12), horaires, fermetures) == "le salon est fermé le lundi"
assert _raison_fermeture(dt.date(2026, 10, 20), horaires, fermetures) == "fermeture exceptionnelle ce jour-là"

# Scénarios bout en bout décrits par l'utilisateur, recalculés depuis
# AUJOURDHUI (le vrai "aujourd'hui" bouge, donc on fige une date de référence).
assert LIMITE_JOURS_RESERVATION == 90

# "le 1er octobre" (sans année, un mardi 6 octobre 2026) -> doit être signalé comme passé
date_1er_oct = _resoudre_jour_mois(1, 10, None, AUJOURDHUI)
assert date_1er_oct < AUJOURDHUI

# "demain" -> decalage_jours=1, pas de résolution jour/mois ici mais vérifiable directement
assert AUJOURDHUI + dt.timedelta(days=1) == dt.date(2026, 10, 7)

# "samedi prochain" -> prochaine occurrence à venir, pas aujourd'hui
assert _prochain_jour_semaine("samedi", AUJOURDHUI) > AUJOURDHUI

# "lundi" -> fermé d'après les horaires de test ci-dessus
prochain_lundi = _prochain_jour_semaine("lundi", AUJOURDHUI)
assert _raison_fermeture(prochain_lundi, horaires, fermetures) == "le salon est fermé le lundi"

# "le 1er octobre 2027" -> plus de 90 jours dans le futur, doit être refusé
date_2027 = _resoudre_jour_mois(1, 10, 2027, AUJOURDHUI)
assert date_2027 > AUJOURDHUI + dt.timedelta(days=LIMITE_JOURS_RESERVATION)

print("OK — toutes les assertions ont passé.")
