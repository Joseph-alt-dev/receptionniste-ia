"""Self-check : un salon connecté en OAuth doit TOUJOURS utiliser son propre
compte Google plutôt que le compte de service, même s'il est aussi marqué
est_demo=True (voir bug du 6 oct 2026 : Belle Étoile, est_demo=True et
connecté en OAuth, réservait dans l'agenda caché du compte de service).

Usage : python3 test_construire_client_calendrier.py
"""

from types import SimpleNamespace
from unittest.mock import patch

import salon_bot
from backend.crypto import chiffrer


class FauxReponse:
    def __init__(self, ok, data=None):
        self.ok = ok
        self._data = data or {}

    def json(self):
        return self._data


def faux_salon(**kwargs):
    base = dict(id=1, calendrier_connecte=False, google_refresh_token=None, google_calendar_id=None, est_demo=False)
    base.update(kwargs)
    return SimpleNamespace(**base)


with (
    patch("salon_bot.requests.post", return_value=FauxReponse(True, {"access_token": "faux-access-token"})),
    patch("salon_bot.build_google_service", side_effect=lambda *_a, credentials, **_k: credentials),
    patch.object(salon_bot.GoogleServiceCredentials, "from_service_account_file", return_value="COMPTE_SERVICE_SENTINEL"),
):
    # OAuth connecté + est_demo=True -> l'OAuth doit l'emporter
    salon = faux_salon(
        calendrier_connecte=True,
        google_refresh_token=chiffrer("faux-refresh-token"),
        google_calendar_id="primary",
        est_demo=True,
    )
    service, calendar_id = salon_bot.construire_client_calendrier(salon)
    assert calendar_id == "primary"
    assert isinstance(service, salon_bot.GoogleOAuthCredentials), (
        f"un salon connecté en OAuth doit utiliser son propre compte même si est_demo=True (a utilisé {service!r})"
    )
    assert service.token == "faux-access-token"

    # est_demo=True sans OAuth connecté -> repli sur le compte de service
    salon_demo_seul = faux_salon(est_demo=True)
    service, calendar_id = salon_bot.construire_client_calendrier(salon_demo_seul)
    assert service == "COMPTE_SERVICE_SENTINEL"

    # ni OAuth ni démo -> rien d'utilisable
    salon_sans_calendrier = faux_salon()
    assert salon_bot.construire_client_calendrier(salon_sans_calendrier) == (None, None)

print("OK — l'OAuth connecté passe toujours avant le compte de service, même pour un salon est_demo=True.")
