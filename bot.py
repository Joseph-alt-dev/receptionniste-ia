import datetime as dt
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv #permet de lire le fichier .env qui contient les clés API
from google.oauth2.service_account import Credentials as GoogleCredentials
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

load_dotenv(override=True) #lis .env et injecte chaque clé dans os.environ le override=True siginfie que si la variable (ailleurs une autre variable du nom de clé groq)existe déjà on l'écrase avec le .env

transport_params = {
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
    ),
}#quand on appelle la clé associée à une fonction lambda ça crée un objet Transport Params qui autorise l'entrée et la sortie audio

SYSTEM_PROMPT = (
    "Tu es Claire, la réceptionniste vocale du salon de coiffure 'Belle Étoile', "
    "situé dans le 15e arrondissement de Paris. "
    "Horaires : du mardi au samedi, de 9h à 19h. Fermé dimanche et lundi. "
    "Quand un client pose une question sur les tarifs ou une prestation, "
    "identifie d'abord s'il s'agit d'une femme, d'un homme, ou d'un enfant "
    "(demande-le si ce n'est pas clair), puis utilise consulter_tarifs pour "
    "obtenir les tarifs exacts avant de répondre. Ne jamais inventer un prix. "
    "Tu aides aussi les clients à prendre rendez-vous en utilisant les fonctions "
    "disponibles : vérifie toujours la disponibilité avec verifier_disponibilite "
    "avant de proposer un créneau, puis confirme la réservation avec "
    "reserver_creneau une fois que le client a choisi. "
    "Si le client veut annuler un rendez-vous existant, utilise annuler_rendez_vous. "
    "Si une demande sort de ton cadre (réclamation, urgence, demande complexe), "
    "utilise escalader_vers_humain. "
    "Reste brève et naturelle, comme dans une vraie conversation téléphonique, "
    "sans emojis ni formatage puisque tes réponses seront lues à voix haute."
    "N'utilise escalader_vers_humain qu'en tout dernier recours, après avoir "
    "essayé toutes les autres solutions. Par exemple : si un créneau n'est pas "
    "disponible, propose une autre date ou heure avant d'escalader. Si une "
    "prestation demandée n'existe pas exactement dans le catalogue, propose la "
    "prestation la plus proche ou demande une précision au client. N'escalade "
    "que pour une vraie réclamation, une urgence, une demande explicite du "
    "client de parler à un humain, ou une situation clairement hors de ton "
    "périmètre (par exemple une question médicale ou une allergie nécessitant "
    "un avis professionnel). "
)

TARIFS = {
    "femme": {
        "coupe_cheveux_courts": 35,
        "coupe_cheveux_mi_longs": 40,
        "coupe_cheveux_longs": 45,
        "brushing_court": 20,
        "brushing_mi_long": 25,
        "brushing_long": 30,
        "couleur_racines": 45,
        "couleur_complete": 60,
        "meches_balayage": 80,
        "soin_keratine": 50,
        "chignon_coiffure_evenementielle": 55,
    },
    "homme": {
        "coupe_classique": 25,
        "coupe_degradee": 28,
        "coupe_et_barbe": 35,
        "taille_barbe_seule": 15,
        "coloration_homme": 30,
    },
    "enfant": {
        "coupe_moins_10_ans": 18,
        "coupe_10_a_14_ans": 22,
    },
}



GOOGLE_CALENDAR_ID = "joseph.quesne@ensae.fr"
GOOGLE_CREDENTIALS_FILE = Path(__file__).parent / "google-credentials.json"
PARIS_TZ = ZoneInfo("Europe/Paris")
HEURE_OUVERTURE = 9
HEURE_FERMETURE = 19
JOURS_OUVERTS = {1, 2, 3, 4, 5}  # date.weekday() : mardi=1 ... samedi=5 (fermé dimanche=6, lundi=0)

google_calendar = build_google_service(
    "calendar",
    "v3",
    credentials=GoogleCredentials.from_service_account_file(
        str(GOOGLE_CREDENTIALS_FILE), scopes=["https://www.googleapis.com/auth/calendar"]
    ),
)


async def consulter_tarifs(params: FunctionCallParams, categorie: str): #fonctions des tarifs
    """Renvoie la liste complète des prestations et tarifs du salon pour 
    une catégorie de client donnée.

    Args:
        categorie: La catégorie du client, parmi "femme", "homme" ou "enfant".
    """
    categorie = categorie.lower().strip()
    if categorie not in TARIFS:
        await params.result_callback({
            "trouve": False,
            "message": f"Catégorie inconnue : {categorie}. Catégories valides : femme, homme, enfant.",
        })
        return

    await params.result_callback({
        "trouve": True,
        "categorie": categorie,
        "tarifs": TARIFS[categorie],
    })

def _creneaux_du_jour(date: str) -> list[dt.datetime] | None:
    """Créneaux d'1h ouverts ce jour-là, ou None si le salon est fermé."""
    jour = dt.date.fromisoformat(date)
    if jour.weekday() not in JOURS_OUVERTS:
        return None
    return [
        dt.datetime.combine(jour, dt.time(heure), tzinfo=PARIS_TZ)
        for heure in range(HEURE_OUVERTURE, HEURE_FERMETURE)
    ]


async def verifier_disponibilite(params: FunctionCallParams, date: str):
    """Vérifie les créneaux disponibles à une date donnée.

    Args:
        date: La date au format AAAA-MM-JJ, par exemple "2026-09-30".
    """
    try:
        creneaux = _creneaux_du_jour(date)
        if creneaux is None:
            await params.result_callback({
                "disponible": False,
                "message": f"Le salon est fermé le {date} (ouvert du mardi au samedi).",
            })
            return

        occupations = google_calendar.freebusy().query(body={
            "timeMin": creneaux[0].isoformat(),
            "timeMax": (creneaux[-1] + dt.timedelta(hours=1)).isoformat(),
            "timeZone": "Europe/Paris",
            "items": [{"id": GOOGLE_CALENDAR_ID}],
        }).execute()["calendars"][GOOGLE_CALENDAR_ID]["busy"]

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
    try:
        debut = dt.datetime.combine(dt.date.fromisoformat(date), dt.time.fromisoformat(heure), tzinfo=PARIS_TZ)
        titre = f"{nom_client} — {prestation}" if prestation else nom_client

        evenement = google_calendar.events().insert(calendarId=GOOGLE_CALENDAR_ID, body={
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
    try:
        debut_journee = dt.datetime.combine(dt.date.fromisoformat(date), dt.time.min, tzinfo=PARIS_TZ)
        evenements = google_calendar.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
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

        google_calendar.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=evenement["id"]).execute()
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



async def run_bot(transport: BaseTransport, runner_args: RunnerArguments):
    logger.info("Starting bot") #écris ce message dans le terminal pour m'informer du lancement du fichier

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

    llm = OpenAILLMService(
        api_key=os.environ["GROQ_API_KEY"], 
        base_url="https://api.groq.com/openai/v1",
        model="openai/gpt-oss-20b",
        settings=OpenAILLMService.Settings(system_instruction=SYSTEM_PROMPT), #le bot parle à groq pas à OPEN AI
    )

    context = LLMContext(tools=[verifier_disponibilite, reserver_creneau, annuler_rendez_vous, consulter_tarifs, escalader_vers_humain]) #les fonctions à tester avant quelconque conversation
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


async def bot(runner_args: RunnerArguments):
    transport = await create_transport(runner_args, transport_params) #va chercher l'entrer au début du dictionnaire
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main
    main()