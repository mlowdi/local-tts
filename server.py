"""Offline Kokoro synthesis, with an OpenAI-style WAV endpoint."""

from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Literal

import onnxruntime as ort
import soundfile as sf
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from kokoro_onnx import Kokoro
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parent
synthesis_lock = Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    options = ort.SessionOptions()
    # Leave CPU capacity for the LLM, and don't spin while idle.
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    session = ort.InferenceSession(
        str(ROOT / "models/kokoro-v1.0.onnx"),
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )
    engine = Kokoro.from_session(session, str(ROOT / "models/voices-v1.0.bin"))
    app.state.engine = engine
    # This interface supports English; other languages need their own G2P setup.
    app.state.voices = {
        name: "en-us" if name.startswith("a") else "en-gb"
        for name in sorted(engine.voices.files)
        if name.startswith(("af_", "am_", "bf_", "bm_"))
    }
    try:
        yield
    finally:
        engine.voices.close()


app = FastAPI(title="Local Kokoro TTS", lifespan=lifespan, docs_url=None, redoc_url=None)


class SpeechRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    model: Literal["kokoro"] = "kokoro"
    input: str = Field(min_length=1, max_length=10000)
    voice: str = "af_heart"
    speed: float = Field(default=1.0, ge=0.5, le=2.0, allow_inf_nan=False)
    response_format: Literal["wav"] = "wav"


@app.get("/health")
def health():
    return {"status": "ok", "model": "kokoro", "device": "cpu"}


@app.get("/v1/models")
def models():
    return {
        "object": "list",
        "data": [{"id": "kokoro", "object": "model", "owned_by": "local"}],
    }


@app.get("/v1/audio/voices")
def voices():
    return {
        "voices": [
            {"id": name, "language": language}
            for name, language in app.state.voices.items()
        ]
    }


@app.post("/v1/audio/speech")
def speech(request: SpeechRequest):
    language = app.state.voices.get(request.voice)
    if language is None:
        raise HTTPException(422, "Unknown voice; see GET /v1/audio/voices")
    # Bound memory use and serialize access to the shared speech engine.
    with synthesis_lock:
        try:
            samples, sample_rate = app.state.engine.create(
                request.input, voice=request.voice, speed=request.speed, lang=language
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        output = BytesIO()
        sf.write(output, samples, sample_rate, format="WAV", subtype="PCM_16")
    return Response(output.getvalue(), media_type="audio/wav")
