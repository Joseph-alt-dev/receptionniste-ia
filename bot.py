import os

from dotenv import load_dotenv #permet de lire le fichier .env qui contient les clés API
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

from backend.database import SessionLocal
from backend.models import Salon
from salon_bot import charger_salon, charger_salon_demo, construire_client_calendrier, construire_system_prompt, construire_tools

load_dotenv(override=True) #lis .env et injecte chaque clé dans os.environ le override=True siginfie que si la variable (ailleurs une autre variable du nom de clé groq)existe déjà on l'écrase avec le .env

transport_params = {
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
    ),
}#quand on appelle la clé associée à une fonction lambda ça crée un objet Transport Params qui autorise l'entrée et la sortie audio


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