"""Speech in and out for the voice channels (ported from Verlox's transcription and voice providers).

Transcribers: Whisper-style models over litellm (OpenAI `whisper-1`, Groq, any OpenAI-compatible host), and any chat
model that accepts audio (OpenRouter's Gemini, Qwen Omni and others, or Gemini directly), asked for an exact
transcript. Speakers: OpenAI-compatible text-to-speech over litellm (OpenAI, SiliconFlow fish-speech, ...) and
ElevenLabs. Without configuration the fakes apply: no transcript (the browser then uses its own speech recognition)
and no audio (the browser speaks the reply itself)."""

from __future__ import annotations

import base64
import os
from abc import ABC, abstractmethod

from keelchat.logging import get_logger

log = get_logger("voice")
MAX_AUDIO_BYTES = 5 * 1024 * 1024


class VoiceError(Exception):
    """A provider failed. The message is for logs; people are asked to type or try again."""


class Transcriber(ABC):
    @abstractmethod
    async def transcribe(self, audio: bytes, fmt: str) -> str:
        """`fmt`: the audio's format, e.g. "wav", "mp3", "webm"."""


class Speaker(ABC):
    #: MIME type of what `speak` returns
    mime = "audio/mpeg"

    @abstractmethod
    async def speak(self, text: str) -> bytes: ...


class FakeTranscriber(Transcriber):
    def __init__(self, transcript: str = "") -> None:
        self.transcript = transcript

    async def transcribe(self, audio: bytes, fmt: str) -> str:
        return self.transcript


class FakeSpeaker(Speaker):
    async def speak(self, text: str) -> bytes:
        return b""


class WhisperTranscriber(Transcriber):
    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None):
        self.model, self.api_key, self.base_url = model, api_key, base_url

    async def transcribe(self, audio: bytes, fmt: str) -> str:
        import io

        import litellm

        clip = io.BytesIO(audio)
        clip.name = f"audio.{fmt}"  # the format is read from the file name
        try:
            r = await litellm.atranscription(model=self.model, file=clip, api_key=self.api_key, api_base=self.base_url, timeout=60, num_retries=0)
        except Exception as e:  # litellm raises provider-specific types
            raise VoiceError(f"{self.model}: {e.__class__.__name__}: {e}") from e
        return str(getattr(r, "text", "") or "").strip()


class ChatAudioTranscriber(Transcriber):
    """An audio-capable chat model asked for an exact transcript (OpenRouter `openrouter/google/gemini-...`,
    `gemini/...`, `openai/gpt-4o-audio-preview`, ...). Formats: wav or mp3."""

    PROMPT = "Transcribe this audio exactly as spoken, in its original language. Reply with the transcript only, nothing else."

    def __init__(self, model: str, api_key: str | None = None):
        self.model, self.api_key = model, api_key

    async def transcribe(self, audio: bytes, fmt: str) -> str:
        import litellm

        if fmt not in ("wav", "mp3"):
            raise VoiceError(f"{self.model} takes wav or mp3, not {fmt}")
        content = [
            {"type": "text", "text": self.PROMPT},
            {"type": "input_audio", "input_audio": {"data": base64.b64encode(audio).decode(), "format": fmt}},
        ]
        try:
            r = await litellm.acompletion(model=self.model, messages=[{"role": "user", "content": content}], api_key=self.api_key, timeout=60, max_tokens=800)
        except Exception as e:
            raise VoiceError(f"{self.model}: {e.__class__.__name__}: {e}") from e
        return str(r.choices[0].message.content or "").strip()


class LiteLLMSpeaker(Speaker):
    """OpenAI-compatible text-to-speech: `openai/tts-1` (voice alloy, nova, ...), or another host by base URL, e.g.
    SiliconFlow `openai/fishaudio/fish-speech-1.5` with voice `fishaudio/fish-speech-1.5:alex`."""

    def __init__(self, model: str, voice: str, api_key: str | None = None, base_url: str | None = None):
        self.model, self.voice, self.api_key, self.base_url = model, voice, api_key, base_url

    async def speak(self, text: str) -> bytes:
        import litellm

        try:
            r = await litellm.aspeech(model=self.model, voice=self.voice, input=text, api_key=self.api_key, api_base=self.base_url, response_format="mp3")
        except Exception as e:
            raise VoiceError(f"{self.model}: {e.__class__.__name__}: {e}") from e
        return bytes(r.content if hasattr(r, "content") else r.read())


class ElevenLabsSpeaker(Speaker):
    def __init__(self, api_key: str, voice_id: str, model: str = "eleven_flash_v2_5"):
        self.api_key, self.voice_id, self.model = api_key, voice_id, model

    async def speak(self, text: str) -> bytes:
        import httpx

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}?output_format=mp3_44100_128"
        try:
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.post(url, json={"text": text, "model_id": self.model}, headers={"xi-api-key": self.api_key})
                r.raise_for_status()
        except httpx.HTTPError as e:
            raise VoiceError(f"elevenlabs: {e}") from e
        return r.content


def voice_from_env() -> tuple[Transcriber, Speaker]:
    """VOICE_STT_MODEL: `whisper-1`, `groq/whisper-large-v3-turbo` (Whisper-style) or an audio chat model such as
    `openrouter/google/gemini-3.8-flash` or `gemini/gemini-2.5-flash`. VOICE_TTS_MODEL: `openai/tts-1`, `elevenlabs`, or a
    model on VOICE_TTS_BASE_URL (+ VOICE_TTS_API_KEY), with VOICE_TTS_VOICE. Keys otherwise come from each provider's
    usual variable (OPENAI_API_KEY, OPENROUTER_API_KEY, GEMINI_API_KEY, ...)."""
    stt: Transcriber = FakeTranscriber()
    if model := os.getenv("VOICE_STT_MODEL"):
        whisper = "whisper" in model or "transcribe" in model or model.startswith("groq/")
        stt = (
            WhisperTranscriber(model, os.getenv("VOICE_STT_API_KEY"), os.getenv("VOICE_STT_BASE_URL"))
            if whisper
            else ChatAudioTranscriber(model, os.getenv("VOICE_STT_API_KEY"))
        )
    tts: Speaker = FakeSpeaker()
    if model := os.getenv("VOICE_TTS_MODEL"):
        if model == "elevenlabs":
            key, voice = os.getenv("ELEVENLABS_API_KEY"), os.getenv("VOICE_TTS_VOICE")
            if not key or not voice:
                raise ValueError("VOICE_TTS_MODEL=elevenlabs needs ELEVENLABS_API_KEY and VOICE_TTS_VOICE (a voice id)")
            tts = ElevenLabsSpeaker(key, voice)
        else:
            tts = LiteLLMSpeaker(model, os.getenv("VOICE_TTS_VOICE") or "alloy", os.getenv("VOICE_TTS_API_KEY"), os.getenv("VOICE_TTS_BASE_URL"))
    log.info("voice", stt=type(stt).__name__, tts=type(tts).__name__)
    return stt, tts
