"""Géocodage d'adresses de salons via Nominatim (OpenStreetMap).

Respecte la politique d'usage de Nominatim : User-Agent identifiable et
une requête par seconde maximum (ponytail: verrou global process-wide,
largement suffisant pour le volume de créations/modifications de salons —
à remplacer par une file si ça devient un goulot). Ne doit jamais faire
échouer l'enregistrement d'un salon : toute erreur renvoie (None, None).
"""

import threading
import time

import requests
from loguru import logger

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "receptionniste-ia (https://github.com/Joseph-alt-dev/receptionniste-ia)"

_verrou = threading.Lock()
_dernier_appel = 0.0


def geocoder_adresse(
    numero_et_rue: str | None, code_postal: str | None, ville: str | None, pays: str | None
) -> tuple[float | None, float | None]:
    if not (numero_et_rue and code_postal and ville):
        return None, None

    global _dernier_appel
    try:
        with _verrou:
            attente = 1.0 - (time.monotonic() - _dernier_appel)
            if attente > 0:
                time.sleep(attente)
            reponse = requests.get(
                NOMINATIM_URL,
                params={
                    "street": numero_et_rue,
                    "postalcode": code_postal,
                    "city": ville,
                    "country": pays or "France",
                    "format": "json",
                    "limit": 1,
                },
                headers={"User-Agent": USER_AGENT},
                timeout=5,
            )
            _dernier_appel = time.monotonic()

        if not reponse.ok:
            logger.warning(f"Géocodage Nominatim : réponse {reponse.status_code} pour {ville!r}")
            return None, None

        resultats = reponse.json()
        if not resultats:
            return None, None
        return float(resultats[0]["lat"]), float(resultats[0]["lon"])
    except Exception as e:
        logger.warning(f"Géocodage Nominatim indisponible pour {ville!r} : {e!r}")
        return None, None
