"""Logique "cerveau" partagée entre bot.py (voix, pipecat), simuler_appel.py
(test terminal) et le chat web (backend/main.py) : chargement des salons,
construction du prompt/outils, accès au calendrier Google, et pilotage d'une
conversation texte par tool-calling manuel. Ne dépend pas de pipecat/audio.
"""

import datetime as dt
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from google.oauth2.credentials import Credentials as GoogleOAuthCredentials
from google.oauth2.service_account import Credentials as GoogleServiceCredentials
from googleapiclient.discovery import build as build_google_service
from googleapiclient.errors import HttpError
from loguru import logger
from openai import AsyncOpenAI
from pipecat.adapters.schemas.direct_function import DirectFunctionWrapper
from pipecat.services.llm_service import FunctionCallParams

from backend.adresses import composer_adresse
from backend.config import GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET
from backend.crypto import dechiffrer
from backend.database import SessionLocal
from backend.models import Salon

PARIS_TZ = ZoneInfo("Europe/Paris")
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]  # index = date.weekday()
MOIS = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]  # index = month - 1
GOOGLE_CREDENTIALS_FILE = Path(__file__).parent / "google-credentials.json"  # compte de service du salon de démo
MODELE_LLM = "openai/gpt-oss-20b"


def _decrire_maintenant() -> str:
    """Date/heure actuelle en Europe/Paris, en toutes lettres, pour le prompt système.
    Recalculée à chaque appel : ne jamais mettre en cache dans une variable longue durée.
    """
    maintenant = dt.datetime.now(PARIS_TZ)
    return (
        f"Nous sommes le {JOURS[maintenant.weekday()]} {maintenant.day} {MOIS[maintenant.month - 1]} "
        f"{maintenant.year}, il est {maintenant.strftime('%Hh%M')} (fuseau Europe/Paris)."
    )


LIMITE_JOURS_RESERVATION = 90  # on ne réserve jamais plus loin que ça dans le futur


def _resoudre_jour_mois(jour: int, mois: int, annee: int | None, aujourdhui: dt.date) -> dt.date:
    """Résout jour/mois/année (donnés par le client) en date calendaire complète.

    Si l'année est omise, prend l'année en cours : ne bascule JAMAIS silencieusement
    sur l'année prochaine si cette date est déjà passée cette année, pour que le
    client qui dit "le 1er octobre" après le 1er octobre se fasse dire clairement
    que c'est passé plutôt que de se retrouver avec un rendez-vous dans un an.
    Lève ValueError si jour/mois/année ne forment pas une date valide.
    """
    return dt.date(annee if annee is not None else aujourdhui.year, mois, jour)


def _prochain_jour_semaine(jour_semaine: str, aujourdhui: dt.date) -> dt.date:
    """Prochaine occurrence à venir d'un jour de semaine ("lundi", "samedi"...),
    aujourd'hui inclus s'il correspond. Lève ValueError si jour_semaine est inconnu.
    """
    delta = (JOURS.index(jour_semaine) - aujourdhui.weekday()) % 7
    return aujourdhui + dt.timedelta(days=delta)


def _raison_fermeture(date: dt.date, horaires: dict, fermetures: set[str]) -> str | None:
    """Renvoie pourquoi le salon est fermé ce jour-là, ou None s'il est ouvert."""
    if date.isoformat() in fermetures:
        return "fermeture exceptionnelle ce jour-là"
    if not horaires.get(JOURS[date.weekday()]):
        return f"le salon est fermé le {JOURS[date.weekday()]}"
    return None


def charger_salon(salon_id: int) -> Salon | None:
    """Charge un salon par id, détaché de sa session (utilisable après fermeture)."""
    db = SessionLocal()
    try:
        salon = db.get(Salon, salon_id)
        if salon:
            db.expunge(salon)
        return salon
    finally:
        db.close()


def charger_salon_demo() -> Salon:
    """Le salon de démo sert de repli quand aucun salon_id n'est résolu."""
    db = SessionLocal()
    try:
        salon = db.query(Salon).filter_by(est_demo=True).first()
        if salon is None:
            raise RuntimeError("Aucun salon de démo en base (voir backend/seed.py).")
        db.expunge(salon)
        return salon
    finally:
        db.close()


def construire_system_prompt(salon: Salon, canal: str = "telephone") -> str:
    """Construit le prompt système. À rappeler à chaque message (pas seulement
    à la création de la conversation) pour que la date/heure injectée reste
    fraîche : voir ConversationTexte.tour et le rafraîchissement par tour côté
    bot.py (pipecat).

    canal: "telephone" (bot vocal) ou "chat" (chat web) — change le rôle annoncé
    et la présentation, jamais "vocale" côté chat.
    """
    horaires = json.loads(salon.horaires)
    jours_ouverts = [jour for jour in JOURS if horaires.get(jour)]
    description_horaires = ", ".join(
        f"{jour} {horaires[jour]['ouverture']}-{horaires[jour]['fermeture']}" for jour in jours_ouverts
    ) or "aucun horaire renseigné pour le moment"

    categories = ", ".join(json.loads(salon.prestations).keys()) or "aucune catégorie renseignée"
    adresse_complete = composer_adresse(salon.numero_et_rue, salon.complement, salon.code_postal, salon.ville, salon.pays)
    adresse = f", situé {adresse_complete}" if adresse_complete else ""

    role = "la réceptionniste" if canal == "telephone" else "l'assistante"
    presentation = f"Bonjour, je suis Rachel, {role} du salon {salon.nom}."
    style = (
        "Reste brève et naturelle, comme dans une vraie conversation téléphonique, "
        "sans emojis ni formatage puisque tes réponses seront lues à voix haute. "
        if canal == "telephone" else
        "Reste brève et naturelle, comme dans une vraie conversation écrite, sans emojis. "
    )

    return (
        f"Tu es Rachel, {role} du salon de coiffure '{salon.nom}'{adresse}. "
        f"{_decrire_maintenant()} "
        "Ne calcule JAMAIS toi-même le jour de la semaine d'une date, si une date "
        "est déjà passée, ou si le salon est ouvert ce jour-là : utilise "
        "systématiquement l'outil resoudre_date pour ça avant de proposer ou "
        "confirmer quoi que ce soit, y compris pour des expressions relatives "
        "(\"demain\", \"samedi prochain\", \"lundi\"...). Si le client donne une date "
        "calendaire sans préciser l'année, ne devine pas l'année toi-même : laisse-la "
        "vide dans l'appel à resoudre_date. Si resoudre_date répond est_passee=true, "
        "ne bascule JAMAIS silencieusement sur l'année suivante : dis clairement au "
        "client que cette date est déjà passée (\"Le 1er octobre est déjà passé, "
        "souhaitez-vous une autre date ?\") et propose les prochains créneaux "
        "disponibles. Ne réserve pour l'année suivante que si le client précise "
        "explicitement l'année ou confirme sans ambiguïté après ta relance. Si "
        "resoudre_date répond trop_loin_pour_reserver=true, dis poliment que tu ne "
        "peux pas réserver à plus de 90 jours à l'avance et propose une date plus "
        "proche. Avant de confirmer tout rendez-vous, répète toujours la date "
        "complète avec son jour de semaine (ex : \"mercredi 7 octobre 2026 à 14h\") "
        "pour que le client puisse corriger une erreur de compréhension. Ne propose "
        "et ne confirme jamais un rendez-vous à une date passée, à plus de 90 jours, "
        "ou un jour où le salon est fermé. "
        f"Horaires d'ouverture : {description_horaires}. "
        f"Catégories de prestations : {categories}. "
        "Quand un client pose une question sur les tarifs ou une prestation, "
        "identifie d'abord sa catégorie (demande-le si ce n'est pas clair), puis "
        "utilise consulter_tarifs pour obtenir les tarifs exacts avant de répondre. "
        "Ne jamais inventer un prix. "
        "Tu aides aussi les clients à prendre rendez-vous en utilisant les fonctions "
        "disponibles : résous toujours la date avec resoudre_date, vérifie la "
        "disponibilité avec verifier_disponibilite avant de proposer un créneau, "
        "puis confirme la réservation avec reserver_creneau une fois que le client "
        "a choisi un créneau ET donné son prénom (obligatoire : demande-le s'il ne "
        "l'a pas donné). "
        "Si le client veut annuler un rendez-vous existant, utilise annuler_rendez_vous. "
        "Si l'une de ces fonctions de calendrier répond qu'elle n'est pas disponible "
        "pour une raison technique, informe poliment le client qu'une personne du "
        "salon le rappellera pour confirmer, sans jamais mentionner de problème "
        "technique. Si elle refuse pour une date/heure invalide, passée, un jour "
        "fermé ou un créneau déjà occupé, propose une alternative au client sans "
        "jamais insister sur ce créneau. "
        "Si une demande sort de ton cadre (réclamation, urgence, demande complexe), "
        "utilise escalader_vers_humain. "
        f"{style}"
        "N'utilise escalader_vers_humain qu'en tout dernier recours, après avoir "
        "essayé toutes les autres solutions. Par exemple : si un créneau n'est pas "
        "disponible, propose une autre date ou heure avant d'escalader. Si une "
        "prestation demandée n'existe pas exactement dans le catalogue, propose la "
        "prestation la plus proche ou demande une précision au client. N'escalade "
        "que pour une vraie réclamation, une urgence, une demande explicite du "
        "client de parler à un humain, ou une situation clairement hors de ton "
        "périmètre (par exemple une question médicale ou une allergie nécessitant "
        "un avis professionnel). Au début de la conversation, présente-toi "
        f"brièvement avec une phrase proche de : « {presentation} »"
    )


def construire_client_calendrier(salon: Salon):
    """Renvoie (service_google, calendar_id), ou (None, None) si aucun calendrier n'est utilisable.

    Le salon de démo garde son accès par compte de service ; les autres salons
    passent par leur refresh_token OAuth (déchiffré) une fois calendrier_connecte=True.
    """
    if salon.est_demo:
        credentials = GoogleServiceCredentials.from_service_account_file(
            str(GOOGLE_CREDENTIALS_FILE), scopes=["https://www.googleapis.com/auth/calendar"]
        )
        return build_google_service("calendar", "v3", credentials=credentials), salon.google_calendar_id

    if salon.calendrier_connecte and salon.google_refresh_token:
        reponse = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
                "refresh_token": dechiffrer(salon.google_refresh_token),
                "grant_type": "refresh_token",
            },
            timeout=10,
        )
        if reponse.ok:
            credentials = GoogleOAuthCredentials(token=reponse.json()["access_token"])
            return build_google_service("calendar", "v3", credentials=credentials), salon.google_calendar_id
        logger.error(f"Échec du rafraîchissement du token Google pour le salon {salon.id} : {reponse.text}")

    return None, None


MESSAGE_CALENDRIER_INDISPONIBLE = (
    "Je ne peux pas accéder à l'agenda en ligne pour le moment : je transmets "
    "votre demande, une personne du salon vous rappellera pour confirmer."
)


def construire_tools(salon: Salon, service, calendar_id: str | None):
    """Fabrique les 5 fonctions-outils, fermées sur les données réelles du salon."""

    prestations = json.loads(salon.prestations)
    fermetures = set(json.loads(salon.fermetures_exceptionnelles))
    horaires = json.loads(salon.horaires)

    async def consulter_tarifs(params: FunctionCallParams, categorie: str):
        """Renvoie la liste complète des prestations et tarifs du salon pour
        une catégorie de client donnée.

        Args:
            categorie: La catégorie du client, parmi celles du salon (ex : "femme", "homme", "enfant").
        """
        categorie = categorie.lower().strip()
        if categorie not in prestations:
            await params.result_callback({
                "trouve": False,
                "message": f"Catégorie inconnue : {categorie}. Catégories valides : {', '.join(prestations)}.",
            })
            return

        await params.result_callback({
            "trouve": True,
            "categorie": categorie,
            "tarifs": prestations[categorie],
        })

    async def resoudre_date(
        params: FunctionCallParams,
        decalage_jours: int | None = None,
        jour_semaine: str | None = None,
        jour: int | None = None,
        mois: int | None = None,
        annee: int | None = None,
    ):
        """Résout une expression de date du client en date calendaire complète,
        et indique son jour de semaine, si elle est déjà passée, et si le salon
        est ouvert ce jour-là. À utiliser SYSTÉMATIQUEMENT avant de proposer ou
        confirmer une date : ne calcule jamais toi-même un jour de semaine ou
        une date relative, cet outil le fait à partir de la date du jour serveur.

        Renseigne EXACTEMENT un des trois moyens suivants de désigner la date :
        - decalage_jours pour une expression relative à aujourd'hui.
        - jour_semaine pour un jour de la semaine (renvoie sa prochaine occurrence).
        - jour + mois (+ annee si précisée) pour une date calendaire.

        Args:
            decalage_jours: Nombre de jours depuis aujourd'hui, pour une
                expression relative ("demain"=1, "après-demain"=2,
                "aujourd'hui"=0). Laisse vide si non pertinent.
            jour_semaine: Le jour de semaine mentionné ("lundi", "samedi"...),
                en minuscules et sans accent particulier. Renvoie toujours la
                prochaine occurrence à venir (aujourd'hui compte s'il
                correspond). Laisse vide si non pertinent.
            jour: Le jour du mois, si le client a donné une date calendaire
                (ex : "le 1er octobre" -> jour=1). Laisse vide si non pertinent.
            mois: Le mois correspondant (1-12), si jour est renseigné.
            annee: L'année, UNIQUEMENT si le client l'a explicitement précisée.
        """
        aujourdhui = dt.datetime.now(PARIS_TZ).date()

        if decalage_jours is not None:
            date_resolue = aujourdhui + dt.timedelta(days=decalage_jours)
        elif jour_semaine is not None:
            jour_semaine = jour_semaine.lower().strip()
            if jour_semaine not in JOURS:
                await params.result_callback({
                    "valide": False,
                    "message": f"Jour de semaine inconnu : {jour_semaine}.",
                })
                return
            date_resolue = _prochain_jour_semaine(jour_semaine, aujourdhui)
        elif jour is not None and mois is not None:
            try:
                date_resolue = _resoudre_jour_mois(jour, mois, annee, aujourdhui)
            except ValueError:
                await params.result_callback({"valide": False, "message": "Cette date n'existe pas."})
                return
        else:
            await params.result_callback({
                "valide": False,
                "message": "Précise decalage_jours, jour_semaine, ou jour+mois.",
            })
            return

        await params.result_callback({
            "valide": True,
            "date": date_resolue.isoformat(),
            "jour_semaine": JOURS[date_resolue.weekday()],
            "est_passee": date_resolue < aujourdhui,
            "trop_loin_pour_reserver": date_resolue > aujourdhui + dt.timedelta(days=LIMITE_JOURS_RESERVATION),
            "ouvert": _raison_fermeture(date_resolue, horaires, fermetures) is None,
            "raison_fermeture": _raison_fermeture(date_resolue, horaires, fermetures),
        })

    def _creneaux_du_jour(date: str) -> list[dt.datetime] | None:
        """Créneaux d'1h ouverts ce jour-là, ou None si le salon est fermé."""
        jour = dt.date.fromisoformat(date)
        if _raison_fermeture(jour, horaires, fermetures):
            return None
        plage = horaires.get(JOURS[jour.weekday()])
        if not plage:
            return None
        ouverture = dt.time.fromisoformat(plage["ouverture"])
        fermeture = dt.time.fromisoformat(plage["fermeture"])
        creneaux = []
        heure = dt.datetime.combine(jour, ouverture, tzinfo=PARIS_TZ)
        fin = dt.datetime.combine(jour, fermeture, tzinfo=PARIS_TZ)
        while heure + dt.timedelta(hours=1) <= fin:
            creneaux.append(heure)
            heure += dt.timedelta(hours=1)
        return creneaux or None

    async def verifier_disponibilite(params: FunctionCallParams, date: str):
        """Vérifie les créneaux disponibles à une date donnée.

        Args:
            date: La date au format AAAA-MM-JJ, par exemple "2026-09-30".
        """
        if service is None:
            await params.result_callback({"disponible": False, "message": MESSAGE_CALENDRIER_INDISPONIBLE})
            return
        try:
            jour_obj = dt.date.fromisoformat(date)
        except ValueError:
            await params.result_callback({"disponible": False, "message": "Date invalide, utilise le format AAAA-MM-JJ."})
            return
        if jour_obj < dt.datetime.now(PARIS_TZ).date():
            await params.result_callback({"disponible": False, "message": f"Le {date} est déjà passé."})
            return
        try:
            creneaux = _creneaux_du_jour(date)
            if creneaux is None:
                raison = _raison_fermeture(jour_obj, horaires, fermetures) or "aucun créneau ce jour-là"
                await params.result_callback({
                    "disponible": False,
                    "message": f"Le salon est fermé le {date} ({raison}).",
                })
                return

            occupations = service.freebusy().query(body={
                "timeMin": creneaux[0].isoformat(),
                "timeMax": (creneaux[-1] + dt.timedelta(hours=1)).isoformat(),
                "timeZone": "Europe/Paris",
                "items": [{"id": calendar_id}],
            }).execute()["calendars"][calendar_id]["busy"]

            creneaux_libres = [
                creneau for creneau in creneaux
                if not any(
                    creneau < dt.datetime.fromisoformat(occupation["end"])
                    and creneau + dt.timedelta(hours=1) > dt.datetime.fromisoformat(occupation["start"])
                    for occupation in occupations
                )
            ]

            if not creneaux_libres:
                await params.result_callback({
                    "disponible": False,
                    "message": f"Il n'y a plus de créneau disponible le {date}.",
                })
            else:
                await params.result_callback({
                    "disponible": True,
                    "creneaux": [creneau.strftime("%H:%M") for creneau in creneaux_libres],
                })
        except HttpError as e:
            logger.error(f"Erreur Google Calendar (verifier_disponibilite) : {e}")
            await params.result_callback({
                "disponible": False,
                "message": "Une erreur technique empêche de consulter le calendrier pour le moment.",
            })

    async def reserver_creneau(
        params: FunctionCallParams, date: str, heure: str, nom_client: str, prestation: str = ""
    ):
        """Réserve un créneau pour un client, à une date et une heure précises.

        Args:
            date: La date du rendez-vous au format AAAA-MM-JJ.
            heure: L'heure du rendez-vous au format HH:MM.
            nom_client: Le nom du client qui prend rendez-vous.
            prestation: La prestation demandée, si elle est connue (ex : "coupe femme").
        """
        logger.info(f"reserver_creneau appelé avec date={date!r} heure={heure!r} nom_client={nom_client!r} prestation={prestation!r}")
        nom_client = (nom_client or "").strip()
        if not nom_client:
            await params.result_callback({
                "succes": False,
                "message": "Le prénom du client est obligatoire pour réserver : demande-le avant de confirmer.",
            })
            return
        if service is None:
            await params.result_callback({"succes": False, "message": MESSAGE_CALENDRIER_INDISPONIBLE})
            return
        try:
            jour_obj = dt.date.fromisoformat(date)
            heure_obj = dt.time.fromisoformat(heure)
        except ValueError:
            await params.result_callback({"succes": False, "message": "Date ou heure invalide."})
            return

        debut = dt.datetime.combine(jour_obj, heure_obj, tzinfo=PARIS_TZ)
        fin = debut + dt.timedelta(hours=1)

        if debut < dt.datetime.now(PARIS_TZ):
            await params.result_callback({
                "succes": False,
                "message": f"Le {date} à {heure} est déjà passé, propose une date future.",
            })
            return

        if jour_obj > dt.datetime.now(PARIS_TZ).date() + dt.timedelta(days=LIMITE_JOURS_RESERVATION):
            await params.result_callback({
                "succes": False,
                "message": f"Impossible de réserver à plus de {LIMITE_JOURS_RESERVATION} jours à l'avance, propose une date plus proche.",
            })
            return

        raison = _raison_fermeture(jour_obj, horaires, fermetures)
        if raison:
            await params.result_callback({"succes": False, "message": f"Le salon est fermé le {date} ({raison})."})
            return

        plage = horaires[JOURS[jour_obj.weekday()]]
        ouverture, fermeture = dt.time.fromisoformat(plage["ouverture"]), dt.time.fromisoformat(plage["fermeture"])
        if heure_obj < ouverture or fin.time() > fermeture:
            await params.result_callback({
                "succes": False,
                "message": f"Le salon est ouvert de {plage['ouverture']} à {plage['fermeture']} le {date}, choisis un autre horaire.",
            })
            return

        try:
            occupations = service.freebusy().query(body={
                "timeMin": debut.isoformat(),
                "timeMax": fin.isoformat(),
                "timeZone": "Europe/Paris",
                "items": [{"id": calendar_id}],
            }).execute()["calendars"][calendar_id]["busy"]
            if occupations:
                await params.result_callback({
                    "succes": False,
                    "message": f"Le créneau de {heure} le {date} est déjà occupé, propose un autre horaire.",
                })
                return

            titre = f"{prestation} - {nom_client}" if prestation else nom_client
            corps = {
                "summary": titre,
                "start": {"dateTime": debut.isoformat(), "timeZone": "Europe/Paris"},
                "end": {"dateTime": fin.isoformat(), "timeZone": "Europe/Paris"},
            }
            logger.info(f"Appel Google Calendar events.insert calendarId={calendar_id!r} body={corps}")

            evenement = service.events().insert(calendarId=calendar_id, body=corps).execute()
            logger.info(f"Réponse Google Calendar : id={evenement['id']} start={evenement['start']}")

            await params.result_callback({
                "succes": True,
                "message": f"Rendez-vous confirmé pour {nom_client} le {date} à {heure}.",
                "id_evenement": evenement["id"],
            })
        except HttpError as e:
            logger.error(f"Erreur Google Calendar (reserver_creneau) : {e}")
            await params.result_callback({
                "succes": False,
                "message": "Une erreur technique empêche de confirmer la réservation pour le moment.",
            })

    async def annuler_rendez_vous(params: FunctionCallParams, date: str, heure: str, nom_client: str):
        """Annule un rendez-vous existant à une date et une heure précises.

        Args:
            date: La date du rendez-vous au format AAAA-MM-JJ.
            heure: L'heure du rendez-vous au format HH:MM.
            nom_client: Le nom du client dont il faut annuler le rendez-vous.
        """
        if service is None:
            await params.result_callback({"succes": False, "message": MESSAGE_CALENDRIER_INDISPONIBLE})
            return
        try:
            debut_journee = dt.datetime.combine(dt.date.fromisoformat(date), dt.time.min, tzinfo=PARIS_TZ)
            evenements = service.events().list(
                calendarId=calendar_id,
                timeMin=debut_journee.isoformat(),
                timeMax=(debut_journee + dt.timedelta(days=1)).isoformat(),
                q=nom_client,
            ).execute().get("items", [])

            evenement = next(
                (e for e in evenements if e["start"].get("dateTime", "").startswith(f"{date}T{heure}")),
                None,
            )
            if evenement is None:
                await params.result_callback({
                    "succes": False,
                    "message": f"Aucun rendez-vous trouvé pour {nom_client} le {date} à {heure}.",
                })
                return

            service.events().delete(calendarId=calendar_id, eventId=evenement["id"]).execute()
            await params.result_callback({
                "succes": True,
                "message": f"Le rendez-vous de {nom_client} le {date} à {heure} a été annulé.",
            })
        except HttpError as e:
            logger.error(f"Erreur Google Calendar (annuler_rendez_vous) : {e}")
            await params.result_callback({
                "succes": False,
                "message": "Une erreur technique empêche l'annulation pour le moment.",
            })

    return [resoudre_date, verifier_disponibilite, reserver_creneau, annuler_rendez_vous, consulter_tarifs, escalader_vers_humain]


async def escalader_vers_humain(params: FunctionCallParams, raison: str):
    """Transfère la demande à un membre de l'équipe du salon.
    À UTILISER UNIQUEMENT EN DERNIER RECOURS, après avoir tenté de résoudre
    la situation par d'autres moyens (proposer une alternative, reformuler,
    demander une précision). Réservé aux vraies réclamations, urgences,
    demandes explicites de parler à un humain, ou situations clairement
    hors du périmètre d'une réceptionniste de salon de coiffure.

    Args:
        raison: Explication de pourquoi la demande doit être transférée,
            en précisant ce qui a déjà été essayé avant d'en arriver là.
    """
    logger.warning(f"Escalade vers un humain demandée : {raison}")
    await params.result_callback({
        "message": "Je transmets votre demande à un membre de notre équipe qui vous recontactera rapidement.",
    })


async def appeler_outil(fonction, arguments: dict) -> dict:
    """Exécute une fonction-outil hors pipecat, avec un FunctionCallParams minimal."""
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
    try:
        await fonction(params, **arguments)
    except Exception as e:
        # un outil qui plante sans ça tue silencieusement toute la conversation
        # (la boucle appelante ne voit jamais l'erreur) : on la logue et on la
        # renvoie au LLM comme un échec d'outil normal, pour qu'il informe le client.
        logger.error(f"Échec de l'appel à l'outil {fonction.__name__} avec {arguments} : {e!r}")
        resultat = {"succes": False, "message": "Une erreur technique empêche cette action pour le moment."}
    return resultat


class ConversationTexte:
    """Pilote une conversation texte (tool-calling manuel, sans pipecat/audio)
    pour un salon donné. Un objet = une conversation indépendante : utilisé
    pour une connexion de chat web ou une session du simulateur terminal.
    """

    def __init__(self, salon: Salon, canal: str = "telephone"):
        self.salon = salon
        self.canal = canal
        service, calendar_id = construire_client_calendrier(salon)
        self.outils = construire_tools(salon, service, calendar_id)
        self._outils_par_nom = {f.__name__: f for f in self.outils}
        self._outils_openai = [
            {"type": "function", "function": DirectFunctionWrapper(f).to_function_schema().to_default_dict()}
            for f in self.outils
        ]
        self._client = AsyncOpenAI(api_key=os.environ["GROQ_API_KEY"], base_url="https://api.groq.com/openai/v1")
        self.messages = [{"role": "system", "content": construire_system_prompt(salon, canal)}]

    async def tour(self, message_utilisateur: str | None, on_appel_outil=None) -> str:
        """Envoie un message utilisateur (None pour relancer sans nouveau message),
        exécute les éventuels appels d'outils, renvoie la réponse finale du bot.
        """
        # recalculée à chaque tour pour que la date/heure injectée reste fraîche
        self.messages[0] = {"role": "system", "content": construire_system_prompt(self.salon, self.canal)}
        if message_utilisateur is not None:
            self.messages.append({"role": "user", "content": message_utilisateur})

        while True:
            reponse = await self._client.chat.completions.create(
                model=MODELE_LLM, messages=self.messages, tools=self._outils_openai,
            )
            message = reponse.choices[0].message
            self.messages.append(message.model_dump(exclude_none=True))

            if not message.tool_calls:
                logger.debug("Aucun appel d'outil ce tour : le LLM a répondu directement en texte.")
                return message.content or ""

            for appel in message.tool_calls:
                arguments = json.loads(appel.function.arguments or "{}")
                logger.info(f"Tool call demandé par le LLM : {appel.function.name}({arguments})")
                resultat = await appeler_outil(self._outils_par_nom[appel.function.name], arguments)
                if on_appel_outil:
                    on_appel_outil(appel.function.name, arguments, resultat)
                self.messages.append({"role": "tool", "tool_call_id": appel.id, "content": json.dumps(resultat)})
