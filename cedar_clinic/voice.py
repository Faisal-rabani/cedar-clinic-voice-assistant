import json
import os

from google.genai.types import HttpOptions, HttpRetryOptions
from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    FunctionCallResultProperties, InputAudioRawFrame, InterruptionFrame, LLMFullResponseEndFrame,
    LLMFullResponseStartFrame, LLMTextFrame, OutputAudioRawFrame, TranscriptionFrame, TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair, LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.serializers.base_serializer import FrameSerializer
from pipecat.services.elevenlabs.stt import ElevenLabsRealtimeSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

from .confirmation import Confirmation
from .store import now

IN_RATE = 16000
OUT_RATE = 24000
TOOLS = [
    FunctionSchema(
        name="check_doctor_availability",
        description="Find open slots. Use the returned slot ID for booking.",
        properties={
            "doctor_id": {"type": "string", "enum": ["meera", "arjun", "nisha"]},
            "date": {"type": "string", "description": "Local date in YYYY-MM-DD format."},
        },
        required=[],
    ),
    FunctionSchema(
        name="book_appointment",
        description="Stage a booking or commit it after the next explicit spoken confirmation.",
        properties={"slot_id": {"type": "string"}},
        required=["slot_id"],
    ),
    FunctionSchema(
        name="cancel_appointment",
        description="Stage a cancellation or commit it after the next explicit spoken confirmation.",
        properties={"appointment_id": {"type": "integer"}},
        required=[],
    ),
]

PROMPT = """You are Cedar Clinic's appointment assistant. Use brief, natural English.
You can check availability, book and cancel. All times are in India Standard Time.
Our doctors are Meera Rao (general medicine), Arjun Shah (cardiology), and Nisha Patel (dermatology).
Use tools for all schedule and appointment facts. Offer two or three real times at once.
Identify callers only by the phone number they speak. For booking, obtain a selected slot first.
For booking or cancellation, call the action tool to stage the action. The server uses the latest
complete phone number recognized in the caller's speech. If the caller gave it earlier, use the
action tool directly. Ask for their full number only if the tool says need_phone.
If the action is staged, the server reads the phone number aloud and asks for confirmation.
Make no more tool calls on that turn.
Only once the caller says yes or correct, repeat the same action call with the same arguments.
If the caller changes anything, use the updated details and obtain a new confirmation.
If the tool returns multiple appointments, ask which one before staging a specific cancellation.
Say a booking or cancellation succeeded only if the tool returned ok true.
Stay focused on scheduling. Do not give medical advice or reveal another caller's appointments.
"""


class PCMSerializer(FrameSerializer):
    async def serialize(self, frame):
        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio
        if isinstance(frame, InterruptionFrame):
            return json.dumps({"type": "clear"})
        return None

    async def deserialize(self, data):
        if isinstance(data, bytes) and 0 < len(data) <= 3200 and len(data) % 2 == 0:
            return InputAudioRawFrame(audio=data, sample_rate=IN_RATE, num_channels=1)
        return None


class Transcript(FrameProcessor):
    def __init__(self, emit, confirmation=None):
        super().__init__()
        self.emit = emit
        self.confirmation = confirmation

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if self.confirmation and isinstance(frame, TranscriptionFrame) and frame.text.strip():
                self.confirmation.hear(frame.text)
                await self.emit("caller", text=frame.text)
            elif isinstance(frame, LLMFullResponseStartFrame):
                await self.emit("assistant_start")
            elif isinstance(frame, LLMTextFrame):
                await self.emit("assistant_text", text=frame.text)
            elif isinstance(frame, LLMFullResponseEndFrame):
                await self.emit("assistant_end")
        await self.push_frame(frame, direction)


async def run_call(socket, store):
    # Pipecat's DEBUG logs can include conversation text and tool arguments.
    logger.disable("pipecat")
    confirmation = Confirmation()

    async def emit(kind, **data):
        try:
            await socket.send_json({"type": kind, **data})
        except (OSError, RuntimeError):
            pass

    key = os.environ["ELEVENLABS_API_KEY"]
    stt = ElevenLabsRealtimeSTTService(api_key=key)
    tts = ElevenLabsTTSService(
        api_key=key,
        settings=ElevenLabsTTSService.Settings(
            voice=os.getenv("ELEVENLABS_VOICE_ID") or "JBFqnCBsd6RMkjVDRZzb",
            model="eleven_flash_v2_5",
        ),
    )
    llm = GoogleLLMService(
        api_key=os.environ["GOOGLE_API_KEY"],
        http_options=HttpOptions(retry_options=HttpRetryOptions(
            attempts=3, initial_delay=1.0, max_delay=4.0, http_status_codes=[502, 503, 504],
        )),
        settings=GoogleLLMService.Settings(
            model=os.getenv("GEMINI_MODEL") or "gemini-2.5-flash",
            system_instruction=f"{PROMPT}\nToday's local date is {now().date()}.",
        ),
    )

    async def availability(params):
        await params.result_callback({"slots": store.availability(**params.arguments)})

    async def speak(text):
        await emit("assistant_start")
        await emit("assistant_text", text=text)
        await emit("assistant_end")
        await task.queue_frame(TTSSpeakFrame(text=text, append_to_context=True))

    async def change(params):
        action = params.function_name
        phone = confirmation.latest_phone
        is_booking = action == "book_appointment"
        target = params.arguments.get("slot_id" if is_booking else "appointment_id")
        if is_booking and not isinstance(target, str):
            await params.result_callback({"ok": False, "message": "A valid slot ID is required."})
            return
        confirmed, result = confirmation.request(action, phone, target)
        if not confirmed and result["status"] == "needs_confirmation":
            await params.result_callback(result, properties=FunctionCallResultProperties(run_llm=False))
            operation = "book" if is_booking else "cancel"
            await speak(f"I heard {result['readback']}. Is that correct? Say yes to {operation} the appointment.")
            return
        if confirmed:
            result = store.book(target, phone) if is_booking else store.cancel(phone, target)
            if result["ok"]:
                appointment = result["appointment"]
                await emit("appointment", action=action, appointment=appointment)
                await params.result_callback(result, properties=FunctionCallResultProperties(run_llm=False))
                status = "booked" if is_booking else "cancelled"
                await speak(f"Your appointment with {appointment['doctor']} is {status}.")
                return
        await params.result_callback(result)

    llm.register_function("check_doctor_availability", availability)
    llm.register_function("book_appointment", change)
    llm.register_function("cancel_appointment", change)
    context = LLMContext(tools=ToolsSchema(standard_tools=TOOLS))
    pair = LLMContextAggregatorPair(context, user_params=LLMUserAggregatorParams(
        vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.65)),
    ))
    transport = FastAPIWebsocketTransport(
        websocket=socket,
        params=FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True,
                                      serializer=PCMSerializer(), session_timeout=600),
    )
    task = PipelineTask(
        Pipeline([transport.input(), stt, Transcript(emit, confirmation), pair.user(),
                  llm, Transcript(emit), tts, transport.output(), pair.assistant()]),
        params=PipelineParams(audio_in_sample_rate=IN_RATE, audio_out_sample_rate=OUT_RATE),
        idle_timeout_secs=120,
    )

    @transport.event_handler("on_client_connected")
    async def connected(_transport, _client):
        await emit("ready")
        await speak("Hello, this is Cedar Clinic. How can I help you with an appointment today?")

    @transport.event_handler("on_client_disconnected")
    async def disconnected(_transport, _client):
        await task.cancel()

    @transport.event_handler("on_session_timeout")
    async def timeout(_transport, _client):
        await emit("error", text="The call reached its ten-minute limit.")
        await task.cancel()

    @task.event_handler("on_pipeline_error")
    async def pipeline_error(_task, _frame):
        await emit("error", text="A voice provider returned an error. Check keys, quota, and voice settings.")

    await PipelineRunner(handle_sigint=False).run(task)
