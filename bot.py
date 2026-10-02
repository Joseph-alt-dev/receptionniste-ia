import datetime as dt
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv #permet de lire le fichier .env qui contient les clés API
from google.oauth2.credentials import Credentials as GoogleOAuthCredentials
from google.oauth2.service_account import Credentials as GoogleServiceCredentials
from googleapiclient.discovery import build as build_google_service
from googleapiclient.errors import HttpError
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner
from pipecat.services.llm_service import FunctionCallParams

from backend.config import GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET
from backend.crypto import dechiffrer
from backend.database import SessionLocal
from backend.models import Salon

load_dotenv(override=True) #lis .env et injecte chaque clé dans os.environ le override=True siginfie que si la variable (ailleurs une autre variable du nom de clé groq)existe déjà on l'écrase avec le .env

transport_params = {
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
    ),
}#quand on appelle la clé associée à une fonction lambda ça crée un objet Transport Params qui autorise l'entrée et la sortie audio

PARIS_TZ = ZoneInfo("Europe/Paris")
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]  # index = date.weekday()
GOOGLE_CREDENTIALS_FILE = Path(__file__).parent / "google-credentials.json"  # compte de service du salon de démo


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


def construire_system_prompt(salon: Salon) -> str:
    horaires = json.loads(salon.horaires)
    jours_ouverts = [jour for jour in JOURS if horaires.get(jour)]
    description_horaires = ", ".join(
        f"{jour} {horaires[jour]['ouverture']}-{horaires[jour]['fermeture']}" for jour in jours_ouverts
    ) or "aucun horaire renseigné pour le moment"

    categories = ", ".join(json.loads(salon.prestations).keys()) or "aucune catégorie renseignée"
    adresse = f", situé {salon.adresse}" if salon.adresse else ""

    return (
        f"Tu es Claire, la réceptionniste vocale du salon de coiffure '{salon.nom}'{adresse}. "
        f"Horaires d'ouverture : {description_horaires}. "
        f"Catégories de prestations : {categories}. "
        "Quand un client pose une question sur les tarifs ou une prestation, "
        "identifie d'abord sa catégorie (demande-le si ce n'est pas clair), puis "
        "utilise consulter_tarifs pour obtenir les tarifs exacts avant de répondre. "
        "Ne jamais inventer un prix. "
        "Tu aides aussi les clients à prendre rendez-vous en utilisant les fonctions "
        "disponibles : vérifie toujours la disponibilité avec verifier_disponibilite "
        "avant de proposer un créneau, puis confirme la réservation avec "
        "reserver_creneau une fois que le client a choisi. "
        "Si le client veut annuler un rendez-vous existant, utilise annuler_rendez_vous. "
        "Si l'une de ces fonctions de calendrier répond qu'elle n'est pas disponible, "
        "informe poliment le client qu'une personne du salon le rappellera pour "
        "confirmer, sans jamais mentionner de problème technique. "
        "Si une demande sort de ton cadre (réclamation, urgence, demande complexe), "
        "utilise escalader_vers_humain. "
        "Reste brève et naturelle, comme dans une vraie conversation téléphonique, "
        "sans emojis ni formatage puisque tes réponses seront lues à voix haute. "
        "N'utilise escalader_vers_humain qu'en tout dernier recours, après avoir "
        "essayé toutes les autres solutions. Par exemple : si un créneau n'est pas "
        "disponible, propose une autre date ou heure avant d'escalader. Si une "
        "prestation demandée n'existe pas exactement dans le catalogue, propose la "
        "prestation la plus proche ou demande une précision au client. N'escalade "
        "que pour une vraie réclamation, une urgence, une demande explicite du "
        "client de parler à un humain, ou une situation clairement hors de ton "
        "périmètre (par exemple une question médicale ou une allergie nécessitant "
        "un avis professionnel)."
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

    def _creneaux_du_jour(date: str) -> list[dt.datetime] | None:
        """Créneaux d'1h ouverts ce jour-là, ou None si le salon est fermé."""
        jour = dt.date.fromisoformat(date)
        if date in fermetures:
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
            creneaux = _creneaux_du_jour(date)
            if creneaux is None:
                await params.result_callback({
                    "disponible": False,
                    "message": f"Le salon est fermé le {date}.",
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
        if service is None:
            await params.result_callback({"succes": False, "message": MESSAGE_CALENDRIER_INDISPONIBLE})
            return
        try:
            debut = dt.datetime.combine(dt.date.fromisoformat(date), dt.time.fromisoformat(heure), tzinfo=PARIS_TZ)
            titre = f"{nom_client} — {prestation}" if prestation else nom_client

            evenement = service.events().insert(calendarId=calendar_id, body={
                "summary": titre,
                "start": {"dateTime": debut.isoformat(), "timeZone": "Europe/Paris"},
                "end": {"dateTime": (debut + dt.timedelta(hours=1)).isoformat(), "timeZone": "Europe/Paris"},
            }).execute()

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

    return [verifier_disponibilite, reserver_creneau, annuler_rendez_vous, consulter_tarifs, escalader_vers_humain]


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



async def run_bot(transport: BaseTransport, runner_args: RunnerArguments, salon: Salon):
    logger.info(f"Starting bot pour le salon {salon.id} ({salon.nom})")

    stt = DeepgramSTTService(
        api_key=os.environ["DEEPGRAM_API_KEY"], #va chercher la clé dans les variables d'environnement (.env)
        settings=DeepgramSTTService.Settings(language=Language.FR),
    )

    tts = ElevenLabsTTSService(
        api_key=os.environ["ELEVENLABS_API_KEY"],
        voice_id="Tv8WbQr8X6VbuvZWgSAv", #identifiant de la voix eleven labs Rachel ici
        settings=ElevenLabsTTSService.Settings(
            model="eleven_flash_v2_5",
            language=Language.FR,
        ),
    )

    service, calendar_id = construire_client_calendrier(salon)

    llm = OpenAILLMService(
        api_key=os.environ["GROQ_API_KEY"],
        base_url="https://api.groq.com/openai/v1",
        model="openai/gpt-oss-20b",
        settings=OpenAILLMService.Settings(system_instruction=construire_system_prompt(salon)), #le bot parle à groq pas à OPEN AI
    )

    context = LLMContext(tools=construire_tools(salon, service, calendar_id)) #les fonctions à tester avant quelconque conversation
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()), #détecte quand une phrase se finit pour savoir quel agrégatuer va parler
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True),
        idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")
        context.add_message({"role": "developer", "content": "Présente-toi brièvement au client."})
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
    await runner.add_workers(worker)
    await runner.run()


async def resoudre_salon(runner_args: RunnerArguments) -> Salon:
    """Détermine quel salon répond à cet appel.

    Ordre de résolution :
    1. call_data.to_number — le numéro Twilio composé (branchement futur, cf. Salon.numero_twilio).
    2. body["salon_id"] — utilisé par le simulateur local (simuler_appel.py) avant que Twilio existe.
    3. Repli sur le salon de démo, pour ne jamais casser le flux de test webrtc existant.
    """
    numero_appele = runner_args.call_data.to_number if runner_args.call_data else None
    if numero_appele:
        db = SessionLocal()
        try:
            salon = db.query(Salon).filter_by(numero_twilio=numero_appele).first()
            if salon:
                db.expunge(salon)
                return salon
        finally:
            db.close()
        logger.warning(f"Aucun salon pour le numéro {numero_appele}, repli sur le salon de démo.")

    salon_id = runner_args.body.get("salon_id") if isinstance(runner_args.body, dict) else None
    if salon_id:
        salon = charger_salon(int(salon_id))
        if salon:
            return salon
        logger.warning(f"Salon {salon_id} introuvable, repli sur le salon de démo.")

    return charger_salon_demo()


async def bot(runner_args: RunnerArguments):
    transport = await create_transport(runner_args, transport_params) #va chercher l'entrer au début du dictionnaire
    salon = await resoudre_salon(runner_args)
    await run_bot(transport, runner_args, salon)


if __name__ == "__main__":
    from pipecat.runner.run import main
    main()