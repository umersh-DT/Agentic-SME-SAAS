import base64
import logging
import os
from typing import Any, Dict

import httpx

try:
    import litellm
except ImportError:
    litellm = None

logger = logging.getLogger("voice_transcription")

TRANSCRIBE_INSTRUCTION = (
    "Transcribe this voice note exactly as spoken, in its original language. "
    "Return only the transcript text with no commentary. If there is no speech, return nothing."
)


class TranscriptionError(Exception):
    pass


async def transcribe_audio(
    audio: bytes, mime_type: str, model_name: str, settings: Dict[str, Any]
) -> Dict[str, Any]:
    """Turns a voice note into text with the configured AI provider.

    Returns {"text", "model", "prompt_tokens", "completion_tokens", "cost_usd"}.
    Gemini models receive the audio directly; OpenAI models use the transcription endpoint.
    """
    voice_cfg = settings.get("voice", {})
    provider = model_name.split("/", 1)[0]
    if provider == "gemini":
        return await _transcribe_with_gemini(audio, mime_type, model_name, voice_cfg)
    if provider == "openai":
        return await _transcribe_with_openai(audio, mime_type, voice_cfg)
    raise TranscriptionError(f"Voice notes are not supported for model provider '{provider}'.")


async def _transcribe_with_gemini(
    audio: bytes, mime_type: str, model_name: str, voice_cfg: Dict[str, Any]
) -> Dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise TranscriptionError("GEMINI_API_KEY is not set.")
    model_id = model_name.split("/", 1)[1]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
    payload = {
        "contents": [{
            "parts": [
                # WhatsApp sends "audio/ogg; codecs=opus"; Gemini expects the bare type.
                {"inline_data": {"mime_type": mime_type.split(";")[0].strip() or "audio/ogg",
                                 "data": base64.b64encode(audio).decode("ascii")}},
                {"text": TRANSCRIBE_INSTRUCTION},
            ]
        }],
        "generationConfig": {"temperature": 0},
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload, headers={"x-goog-api-key": api_key})
    if resp.status_code != 200:
        raise TranscriptionError(f"Gemini transcription failed with HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    usage = data.get("usageMetadata") or {}
    prompt_tokens = int(usage.get("promptTokenCount", 0))
    completion_tokens = int(usage.get("candidatesTokenCount", 0))
    rates = voice_cfg.get("gemini_rates_per_1m_tokens", {})
    cost = (prompt_tokens * float(rates.get("audio_input_usd", 1.00))
            + completion_tokens * float(rates.get("output_usd", 2.50))) / 1_000_000
    return {"text": text, "model": model_name, "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens, "cost_usd": cost}


async def _transcribe_with_openai(audio: bytes, mime_type: str, voice_cfg: Dict[str, Any]) -> Dict[str, Any]:
    if litellm is None:
        raise TranscriptionError("litellm is not installed.")
    model = voice_cfg.get("openai_transcription_model", "whisper-1")
    response = await litellm.atranscription(
        model=model, file=("voice-note.ogg", audio, mime_type or "audio/ogg"), response_format="verbose_json"
    )
    text = (getattr(response, "text", "") or "").strip()
    duration = float(getattr(response, "duration", 0) or 0)
    if not duration:
        logger.warning("[VOICE] Transcription duration unknown; cost recorded as 0.")
    cost = duration / 60.0 * float(voice_cfg.get("openai_usd_per_minute", 0.006))
    return {"text": text, "model": f"openai/{model}", "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": cost}
