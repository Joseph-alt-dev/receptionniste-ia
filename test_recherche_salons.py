"""Self-check pour la recherche tolérante et le filtre de complétude des
salons publics (backend/main.py). Usage : python3 test_recherche_salons.py
"""

from types import SimpleNamespace

from backend.main import SEUIL_PERTINENCE, _normaliser, _salon_complet, _score_pertinence

salon = SimpleNamespace(
    nom="Belle Étoile",
    ville="Paris",
    code_postal="75015",
    numero_et_rue="12 Rue de la Convention",
    horaires='{"lundi": null, "mardi": {"ouverture": "09:00", "fermeture": "19:00"}}',
    prestations='{"femme": {"Coupe": 30}, "homme": {}}',
)
salon_vide = SimpleNamespace(
    nom="Ciel étoilé", ville=None, code_postal=None, numero_et_rue=None,
    horaires='{"lundi": null}', prestations='{"femme": {}, "homme": {}}',
)

assert _salon_complet(salon) is True
assert _salon_complet(salon_vide) is False

for requete in ("belle etoile", "bele étoile", "belle etoil"):
    assert _score_pertinence(_normaliser(requete), salon) >= SEUIL_PERTINENCE, requete
assert _score_pertinence(_normaliser("xyz"), salon) < SEUIL_PERTINENCE

print("OK — toutes les assertions ont passé.")
