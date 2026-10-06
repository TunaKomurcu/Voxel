"""Spoken debrief: a short, coach-style summary of a finished evaluation, read
aloud by ElevenLabs in the voice of the persona the user just pitched to.

Strictly post-call and optional. It reads the judge's JSON output and never
touches the Voice Agent session, turn detection, or judge.py itself. Without
ELEVENLABS_API_KEY (or the `elevenlabs` package) it reports itself disabled
and the browser never shows the "Play debrief" button.

The text is built from the judge JSON with a fixed template rather than a
second LLM call: no extra latency, nothing to hallucinate, and the word cap
is guaranteed rather than requested.
"""

import importlib.util
import os
from typing import Iterator, Optional

# Persona key (server.py's PERSONAS[].key) -> ElevenLabs voice_id. ElevenLabs
# default voices, so any account has them; swap in any id from your own
# voice library at https://elevenlabs.io/app/voice-library.
DEBRIEF_VOICES = {
    "investor": "nPczCjzI2devNBz1zQrb",    # Marcus -> Brian
    "technical": "XrExE9yKIg1WjnnlVkGX",   # Priya  -> Matilda
    "buyer": "cgSgspJ2msm6clMCkdW9",       # Grace  -> Jessica
    "enterprise": "cjVigY5qzO86Huf0OWal",  # Derek  -> Eric
}
# Single-agent mode (AGENT=<name>) has no persona key.
DEFAULT_VOICE = DEBRIEF_VOICES["investor"]

MODEL_ID = "eleven_flash_v2_5"
OUTPUT_FORMAT = "mp3_44100_64"
MAX_WORDS = 60


def is_enabled() -> bool:
    return bool(os.environ.get("ELEVENLABS_API_KEY")) and importlib.util.find_spec("elevenlabs") is not None


def voice_for(persona_key: Optional[str]) -> str:
    return DEBRIEF_VOICES.get(persona_key or "", DEFAULT_VOICE)


def _sentence(text: str) -> str:
    text = " ".join(str(text).split())
    return text if text.endswith((".", "!", "?")) else text + "."


def _label(key: str) -> str:
    return key.replace("_", " ")


def build_debrief_text(evaluation: dict, speaker: Optional[str] = None,
                       max_words: int = MAX_WORDS) -> str:
    """Score first, then as many of (weakest area, top suggestion, interruption
    recovery, strongest area) as fit under max_words, in that priority. Each
    piece is kept whole or dropped, never cut mid-sentence, since a truncated
    sentence sounds broken when spoken. Output order is the natural one."""
    score = round(evaluation["overall_score"])
    opener = f"{speaker} here. " if speaker else ""
    head = f"{opener}You scored {score} out of 100."

    categories = evaluation["categories"]
    ranked = sorted(categories.items(), key=lambda kv: kv[1]["score"])
    (weak_key, weak), (strong_key, strong) = ranked[0], ranked[-1]

    pieces: dict[str, str] = {}
    if strong["score"] > weak["score"]:
        pieces["strong"] = f"Strongest was {_label(strong_key)}: {_sentence(strong['note'])}"
        pieces["weak"] = f"Work on {_label(weak_key)}: {_sentence(weak['note'])}"
    else:
        pieces["weak"] = _sentence(weak["note"])

    interruptions = evaluation.get("interruptions") or []
    if interruptions:
        recovered = sum(1 for i in interruptions if i.get("recovery_quality") == "strong")
        count = len(interruptions)
        pieces["recovery"] = (f"You recovered well from {recovered} of {count} "
                              f"interruption{'' if count == 1 else 's'}.")

    suggestions = [s for s in evaluation.get("suggestions") or [] if str(s).strip()]
    if suggestions:
        pieces["tip"] = f"Next time: {_sentence(suggestions[0])}"

    budget = max_words - len(head.split())
    kept = set()
    for name in ("weak", "tip", "recovery", "strong"):
        words = len(pieces.get(name, "").split())
        if name in pieces and words <= budget:
            kept.add(name)
            budget -= words

    order = ("recovery", "strong", "weak", "tip")
    return " ".join([head] + [pieces[n] for n in order if n in kept])


def describe_error(err: Exception) -> str:
    """One line for the server log. The SDK's ApiError str() includes every
    response header, which buries the part that matters."""
    status = getattr(err, "status_code", None)
    if status is None:
        return f"{type(err).__name__}: {err}"
    body = getattr(err, "body", None)
    detail = body.get("detail", body) if isinstance(body, dict) else body
    message = detail.get("message", detail) if isinstance(detail, dict) else detail
    return f"HTTP {status}: {message}"


def stream_audio(text: str, voice_id: str) -> Iterator[bytes]:
    """MP3 chunks as ElevenLabs produces them. Lazy: the HTTP request goes out
    on the first next(), so that's where auth/voice errors surface."""
    from elevenlabs.client import ElevenLabs

    client = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
    return client.text_to_speech.stream(
        voice_id, text=text, model_id=MODEL_ID, output_format=OUTPUT_FORMAT,
    )

