"""Session retrieval, interruption detection, and the judge pass.

Standard library only, matching lib.py's "no pip install" rule.

Session retrieval: GET /sessions/{id} does not inline the timeline — it
points to it via a presigned S3 URL in artifacts[type=="timeline"] (and the
recording similarly under artifacts[type=="audio"]), each fetched
separately.
"""

import copy
import json
import re
import time
import urllib.request
from typing import Any, Optional

import lib

# qwen3.5-4b-fast — the only model this account's free tier has LLM Gateway
# access to (confirmed on the dashboard; every Claude model needs a paid
# upgrade). A permanent choice, not a placeholder — reliable in practice
# because of the retry + sanitize safety net in call_judge(). See PLAN.md.
JUDGE_MODEL = "qwen3.5-4b-32k-fast"

JUDGE_SYSTEM_PROMPT = """You are an objective evaluation judge for a practice call between a founder and a skeptical counterpart persona — an investor, a technical co-founder candidate, or a buyer, depending on the session. You did not take part in the call; you are reading a transcript after the fact.

The user message names the specific counterpart for this call (who they are, what they're evaluating). Use that to judge "audience_responsiveness": what counts as a responsive answer depends on what THIS counterpart actually cares about. A technical co-founder wants mechanism-level depth ("how does it actually work," "what breaks at scale") — jargon-free plain language is not what they're asking for. A non-technical buyer wants plain language and gets frustrated by jargon, not by a lack of technical depth. A buyer focused on cost and timeline wants concrete ROI/price/speed, not market size or investor-style traction metrics. Don't import investor-pitch assumptions into a call where the counterpart never asked about those things.

Lines marked (INTERRUPTION: <type>) are moments the counterpart cut the founder off. Some carry a bracketed timing note (pre-interrupt speech rate, response latency, recovery speech rate, in words/sec and milliseconds) — treat it as one more signal about composure, not something to repeat verbatim. The transcript may be followed by a block of sentence-level sentiment analysis; match those sentences to the transcript by their text, not by position, and use them as context for tone, not as a separate topic to discuss.

Regardless of any sentiment context provided, NEVER describe or infer a founder response, tone, or behavior that is not explicitly present in the [Founder] lines of the transcript. If a turn has no [Founder] line, that turn contains no founder response — do not invent one.

The type after the colon tells you which of two distinct mechanisms caused the cut-in — use it to fix "trigger", don't guess from content when the type already answers it:
- "hesitation_cutoff": the founder trailed off and the counterpart's turn-detection treated the pause as the end of their turn. This is not a claim, a number, or a dodge — always use trigger: "hesitation" for these.
- "barge_in": the counterpart talked over the founder mid-sentence because of what was being said. Pick the trigger from the actual content: "vague_claim", "unsupported_number", or "ignored_question".

Only put an entry in "interruptions" for a line that is actually marked (INTERRUPTION: <type>) below. Do not add an entry for any other turn, no matter how weak it was — if nothing in the transcript is marked, "interruptions" must be an empty list, even if the founder said little or nothing.

For each interruption, judge how well the founder recovered in their next turn: did they answer the counterpart's sharp follow-up specifically, or did they stay vague, dodge, or restate the same hand-wavy claim?

Every "recovery_pattern" must be grounded in what was literally said, not assumed. A bare acknowledgment ("okay", "alright", "sure") contains no claim. "recovery_pattern: answered_directly" requires the founder's next turn to state new, concrete information — a number, a name, a specific mechanism. Repeating what they already said, turning the question back on the counterpart, or a bare acknowledgment is "deflected", "repeated_claim", or "asked_clarifying_question" — never "answered_directly".

Content substance and the overall score must track how much real information the founder actually gave, not how politely they behaved. A one-word acknowledgment, a topic change, or turning the question back on the counterpart earns a low content_substance score and a low overall_score — good composure or a pleasant tone never offsets a lack of content. If the founder gave little or no substantive information for the entire call, overall_score must be low — well under 40 — regardless of how the rest of the call went.

`better_response_example` must reference something specific and real from THIS transcript — an exact number, claim, or phrase the founder actually said or should have said instead. Generic coaching sentences ("be more specific", "add more data", "provide concrete numbers") are not acceptable.

Check the transcript for a real number for the metric you're about to cite. If the founder said one, reuse it. If not, you MUST write a bracketed placeholder like [X] instead of inventing a figure — a made-up number reads as real and misleads the person reviewing this.

WRONG (the transcript never gave a number, so "$45" and "$12" are invented): "We hit an LTV of $45 and a CAC of $12 last quarter."
RIGHT (same situation, placeholders instead): "We hit an LTV of $[X] and a CAC of $[Y] last quarter."
RIGHT (the transcript actually said "we charge ten percent per booking"): "Our take rate is 10% per booking, which nets us $[X] per transaction after processing fees."

Before you write better_response_example, silently check: does every number in this sentence appear in the transcript above? If any number does not, replace it with [X], [Y], [Z] before answering.

Return ONLY valid JSON, no prose before or after, matching exactly this shape:
{
  "overall_score": <integer 0-100>,
  "categories": {
    "content_substance": {"score": <0-100>, "note": "<one sentence>"},
    "composure_under_pressure": {"score": <0-100>, "note": "<one sentence>"},
    "audience_responsiveness": {"score": <0-100>, "note": "<one sentence>"}
  },
  "interruptions": [
    {
      "moment": "<short description of what triggered the interruption>",
      "trigger": "vague_claim" | "unsupported_number" | "hesitation" | "ignored_question",
      "recovery_quality": "strong" | "weak" | "poor",
      "recovery_pattern": "answered_directly" | "deflected" | "repeated_claim" | "asked_clarifying_question",
      "note": "<one concrete sentence about what they said>",
      "better_response_example": "<one sentence, specific to this transcript>"
    }
  ],
  "suggestions": ["<actionable suggestion>", "..."]
}

If there were no interruptions in the transcript, base the scores on how clearly the founder communicated overall, return an empty "interruptions" list, and still give 1-3 specific suggestions grounded in what was actually said. In that case, keep the category notes' language neutral — say "after being asked a direct question" or similar, not "interrupted" or "cut off" — since claiming an interruption in prose while the schema says there were none is confusing.

An empty "interruptions" list does not by itself mean the founder was under no pressure — it only means no reply happened to overlap or cut them off by our timing/text definitions. A counterpart can put real pressure on the founder through the sharpness of their questions alone: a direct, mechanism-level, no-escape-hatch question ("What is the specific mechanism that maps the project requirements to the freelancer profiles?") is pressure whether or not it happened to arrive as an interruption. When scoring "composure_under_pressure", don't default it to high just because "interruptions" is empty. Instead, look at how sharp and specific the counterpart's turns were, and how concrete versus superficial the founder's answers were. If the counterpart asked pointed, challenging questions — repeatedly pressing for a mechanism, a number, or a name — and the founder answered with vague generalities, repeated the same claim, or pivoted to a different topic, that is poor composure under pressure and should score low, consistent with low content_substance/audience_responsiveness, even with zero interruptions. Never interrupted is not the same as never under pressure."""


# --- session + timeline ------------------------------------------------------


def fetch_session(session_id: str) -> dict:
    return lib.aai(f"/sessions/{session_id}")


def _artifact_url(session: dict, artifact_type: str) -> Optional[str]:
    artifact = next((a for a in session.get("artifacts", []) if a.get("type") == artifact_type), None)
    return artifact["url"] if artifact else None


def fetch_timeline(session_id: str) -> dict:
    session = fetch_session(session_id)
    url = _artifact_url(session, "timeline")
    if url is None:
        raise ValueError(f"Session {session_id} has no timeline artifact yet (status={session.get('status')})")
    with urllib.request.urlopen(url) as res:
        return json.loads(res.read().decode())


def _is_barge_in(turn: dict) -> bool:
    """The agent's reply started at or before the moment AssemblyAI's own
    turn-detection marked the user's speech as ended — a real audio-level
    barge-in, confirmed against a live session (the "Uber of pet care" call
    in sample_session_timeline.json)."""
    started = turn.get("agent_reply_started_at_ms")
    ended = turn.get("user_speech_ended_at_ms")
    if started is None or ended is None:
        return False
    return started <= ended


def _is_hesitation_cutoff(turn: dict) -> bool:
    """The transcript trails off without a terminal punctuation mark —
    catches AssemblyAI's silence-timeout turn-taking cutting a founder off
    mid-thought, which _is_barge_in cannot see: confirmed against a real
    session (hesitation_session.json) that the agent's reply always starts
    several hundred ms to ~1.5s after the last detected speech, timeout or
    not — the delay ranges for a genuine cutoff and a normal, un-interrupted
    reply overlap almost exactly (see TESTING.md), so elapsed time alone
    can't tell them apart. Text completeness is the signal that does.

    Risk: this depends on the ASR's punctuation being reliable. A short,
    naturally unpunctuated answer ("Yes", "No") could in principle read as
    a false-positive hesitation cutoff. Not observed in any fixture so far
    (every genuinely complete turn across all fixtures ends in . ? or !),
    but worth watching for as more real calls come in.
    """
    transcript = (turn.get("user_transcript") or "").strip()
    return bool(transcript) and transcript[-1] not in ".?!"


def _interruption_type(turn: dict) -> Optional[str]:
    """"barge_in", "hesitation_cutoff", or None — two distinct mechanisms
    the investor persona can cut the founder off with, kept separate so the
    judge prompt can tell them apart (see build_judge_prompt) instead of
    guessing a trigger from content alone."""
    if turn.get("trigger") != "user_speech":
        return None
    if _is_barge_in(turn):
        return "barge_in"
    if _is_hesitation_cutoff(turn):
        return "hesitation_cutoff"
    return None


def _is_interruption(turn: dict) -> bool:
    return _interruption_type(turn) is not None


def parse_timeline(timeline: dict) -> list[dict]:
    turns = []
    for turn in timeline.get("turns", []):
        itype = _interruption_type(turn)
        turns.append({**turn, "is_interruption": itype is not None, "interruption_type": itype})
    return turns


def interruption_turns(turns: list[dict]) -> list[dict]:
    return [t for t in turns if t["is_interruption"]]


def _no_user_speech(turns: list[dict]) -> bool:
    """True if the founder never said anything at all — every turn's
    user_transcript is null, empty, or whitespace-only (an empty list of
    turns counts too: no data is no speech either). Confirmed against a
    real call (sess_fa8d263aa9294da8866e4e8d04a3473e, see
    zero_response_session.json): qwen3.5-4b-32k-fast hallucinated a
    fabricated founder response and a 65-70/100 score for this exact case
    2 times out of 3, even though the prompt's own instructions already
    say to score near-zero here — the model just doesn't reliably follow
    them once sentiment context makes the prompt look like a real
    back-and-forth. This check removes the LLM from that decision
    entirely instead of trying to prompt-engineer around it."""
    return all(not (t.get("user_transcript") or "").strip() for t in turns)


_ZERO_RESPONSE_RESULT = {
    "overall_score": 0,
    "categories": {
        "content_substance": {"score": 0, "note": "No response was given."},
        "composure_under_pressure": {"score": 0, "note": "No response was given."},
        "audience_responsiveness": {"score": 0, "note": "No response was given."},
    },
    "interruptions": [],
    "suggestions": ["Start by directly answering the question you were asked."],
}


def _zero_response_result() -> dict:
    return copy.deepcopy(_ZERO_RESPONSE_RESULT)


# --- timing signals -----------------------------------------------------------
#
# A turn where the AGENT's own reply got cut off by the user (status
# "interrupted", agent_text sometimes a stray fragment like "If") does not
# corrupt anything below: _words_per_second only ever reads user_transcript
# and the user's own timestamps, never agent_text; the one agent-side field
# it does use, agent_reply_ended_at_ms, is set correctly by AssemblyAI to
# the true stop time even when that reply was interrupted. Confirmed against
# a real session (tests/fixtures/no_interruption_session.json).


def _words_per_second(transcript: Optional[str], start_ms: Optional[int], end_ms: Optional[int]) -> Optional[float]:
    if not transcript or start_ms is None or end_ms is None or end_ms <= start_ms:
        return None
    word_count = len(transcript.split())
    duration_s = (end_ms - start_ms) / 1000
    return round(word_count / duration_s, 2)


def compute_timing_signals(turns: list[dict]) -> list[dict]:
    """Annotates each interruption turn with response_latency_ms and
    pre/post words_per_second, computed from the timeline's existing
    utterance-level timestamps — there are no word-level timestamps to work
    from. Non-interruption turns pass through unchanged. If an interruption
    is the last turn, there's no recovery turn to measure, so
    response_latency_ms and recovery_wps come back None."""
    annotated = []
    for i, turn in enumerate(turns):
        if not turn.get("is_interruption"):
            annotated.append(turn)
            continue
        pre_interrupt_wps = _words_per_second(
            turn.get("user_transcript"),
            turn.get("user_speech_started_at_ms"),
            turn.get("user_speech_ended_at_ms"),
        )
        response_latency_ms = None
        recovery_wps = None
        next_turn = turns[i + 1] if i + 1 < len(turns) else None
        if next_turn is not None and next_turn.get("trigger") == "user_speech":
            interrupt_end = turn.get("agent_reply_ended_at_ms")
            recovery_start = next_turn.get("user_speech_started_at_ms")
            if interrupt_end is not None and recovery_start is not None:
                response_latency_ms = recovery_start - interrupt_end
            recovery_wps = _words_per_second(
                next_turn.get("user_transcript"),
                next_turn.get("user_speech_started_at_ms"),
                next_turn.get("user_speech_ended_at_ms"),
            )
        annotated.append({
            **turn,
            "response_latency_ms": response_latency_ms,
            "pre_interrupt_wps": pre_interrupt_wps,
            "recovery_wps": recovery_wps,
        })
    return annotated


# --- sentiment ------------------------------------------------------------------


def fetch_sentiment(audio_url: str) -> list[dict]:
    """Runs AssemblyAI's classic pre-recorded transcription API (not the
    Voice Agent API) with sentiment_analysis on, against the session
    recording. Sentence-level results only; timestamps are relative to the
    recording, not the timeline's absolute unix-ms, so they aren't
    reconciled here — see build_judge_prompt."""
    result = lib.wait_for_transcript(audio_url, sentiment_analysis=True)
    return result.get("sentiment_analysis_results") or []


# --- judge pass ----------------------------------------------------------------


def _format_sentiment_context(sentiment_results: list[dict]) -> str:
    lines = [
        f'- "{r["text"]}" -> {r["sentiment"]} (confidence {r["confidence"]:.2f})'
        for r in sentiment_results
    ]
    return (
        "Sentence-level sentiment analysis of the call audio "
        "(match to transcript lines by content, not position):\n" + "\n".join(lines)
    )


def _timing_note(turn: dict) -> str:
    bits = []
    if turn.get("pre_interrupt_wps") is not None:
        bits.append(f"pre-interrupt {turn['pre_interrupt_wps']} words/sec")
    if turn.get("response_latency_ms") is not None:
        bits.append(f"response latency {turn['response_latency_ms']}ms")
    if turn.get("recovery_wps") is not None:
        bits.append(f"recovery {turn['recovery_wps']} words/sec")
    return f" [{'; '.join(bits)}]" if bits else ""


def build_judge_prompt(turns: list[dict], sentiment_results: Optional[list[dict]] = None,
                        counterpart_description: Optional[str] = None) -> list[dict]:
    lines = []
    for turn in turns:
        if turn.get("user_transcript"):
            lines.append(f"[Founder]: {turn['user_transcript']}")
        if turn.get("agent_text"):
            itype = turn.get("interruption_type")
            marker = f" (INTERRUPTION: {itype}){_timing_note(turn)}" if itype else ""
            lines.append(f"[Counterpart{marker}]: {turn['agent_text']}")
    parts = []
    if counterpart_description:
        parts.append(f"Counterpart: {counterpart_description}")
    parts.append(f"Transcript:\n\n{chr(10).join(lines)}")
    content = "\n\n".join(parts)
    if sentiment_results:
        content += "\n\n" + _format_sentiment_context(sentiment_results)
    return [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


_CODE_FENCE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _strip_code_fence(content: str) -> str:
    """Some gateway models wrap JSON in a markdown fence despite instructions
    not to. Strip it if present; leave anything else untouched."""
    match = _CODE_FENCE.match(content)
    return match.group(1) if match else content


_MAX_JSON_ATTEMPTS = 3

_INVALID_JSON_REMINDER = {
    "role": "user",
    "content": ("Your previous reply was not valid JSON matching the required schema. "
                "Return ONLY valid JSON, no markdown code fences, no prose before or after."),
}

_NUMBER_RETRY_REMINDER = {
    "role": "user",
    "content": ("One or more of your better_response_example fields used a specific number "
                "that never appeared in the transcript. Return the full JSON again, unchanged "
                "except: replace any invented numbers in better_response_example with "
                "bracketed placeholders like [X], [Y]."),
}

# $ / % optional around a digit run — matches "$45", "12%", "2,400", "3.5".
_NUMBER_PATTERN = re.compile(r"(\$)?(\d[\d,]*(?:\.\d+)?)(%)?")
_PLACEHOLDER_LETTERS = "XYZWVUTSRQPONMLKJIHGFEDCBA"


def _normalize_number(raw: str) -> str:
    return raw.replace(",", "")


def _extract_numbers(text: str) -> set[str]:
    return {_normalize_number(m.group(2)) for m in _NUMBER_PATTERN.finditer(text)}


def _dialogue_text(turns: list[dict]) -> str:
    """Only what was actually said — not our injected timing/sentiment
    context, which would otherwise make every number look "real"."""
    parts = []
    for turn in turns:
        if turn.get("user_transcript"):
            parts.append(turn["user_transcript"])
        if turn.get("agent_text"):
            parts.append(turn["agent_text"])
    return " ".join(parts)


def _invented_numbers_in_examples(data: dict, allowed_numbers: set[str]) -> set[str]:
    found: set[str] = set()
    for item in data.get("interruptions", []):
        found |= _extract_numbers(item.get("better_response_example", "")) - allowed_numbers
    return found


def sanitize_response_example(text: str, allowed_numbers: set[str]) -> str:
    """Regex fallback for when the judge won't stop inventing numbers even
    after being asked to: replaces any number not in allowed_numbers with a
    bracketed placeholder ([X], [Y], ...), keeping a $ prefix or % suffix."""
    counter = 0

    def replace(match: re.Match) -> str:
        nonlocal counter
        prefix, number, suffix = match.group(1) or "", match.group(2), match.group(3) or ""
        if _normalize_number(number) in allowed_numbers:
            return match.group(0)
        letter = _PLACEHOLDER_LETTERS[counter % len(_PLACEHOLDER_LETTERS)]
        counter += 1
        return f"{prefix}[{letter}]{suffix}"

    return _NUMBER_PATTERN.sub(replace, text)


def _sanitize_all_examples(data: dict, allowed_numbers: set[str]) -> dict:
    sanitized = copy.deepcopy(data)
    for item in sanitized.get("interruptions", []):
        item["better_response_example"] = sanitize_response_example(
            item["better_response_example"], allowed_numbers
        )
    return sanitized


class RateLimitedError(Exception):
    """Raised once the gateway is still answering 429 after every retry.
    Kept separate from the ValueError-based JSON-format retry loop so the
    two concerns (malformed output vs. transient rate limiting) can't get
    tangled into one retry count."""

    def __init__(self, message: str = "Service is busy, please try again in a moment."):
        super().__init__(message)


_MAX_RATE_LIMIT_ATTEMPTS = 3
_RATE_LIMIT_BACKOFF_SECONDS = [5, 15]  # wait before attempt 2, then before attempt 3


def _llm_gateway_chat_with_retry(model: str, messages: list[dict], max_tokens: int) -> dict:
    """Wraps lib.llm_gateway_chat with backoff retry on 429 specifically —
    other ApiErrors (bad request, auth, 5xx) aren't transient in the same
    way and propagate immediately."""
    last_error: Optional[lib.ApiError] = None
    for attempt in range(_MAX_RATE_LIMIT_ATTEMPTS):
        try:
            return lib.llm_gateway_chat(model=model, messages=messages, max_tokens=max_tokens)
        except lib.ApiError as err:
            if err.status != 429:
                raise
            last_error = err
            if attempt < _MAX_RATE_LIMIT_ATTEMPTS - 1:
                time.sleep(_RATE_LIMIT_BACKOFF_SECONDS[attempt])
    raise RateLimitedError() from last_error


def _request_judge_json(messages: list[dict], model: str) -> dict:
    """One gateway round-trip: send messages, parse, validate. Raises
    ValueError (json.JSONDecodeError is a ValueError subclass) on either
    failure, for the retry loop in call_judge to catch."""
    response = _llm_gateway_chat_with_retry(model, messages, max_tokens=1500)
    content = response["choices"][0]["message"]["content"]
    data = json.loads(_strip_code_fence(content))
    validate_judge_output(data)
    return data


_INTERRUPTION_COUNT_RETRY_REMINDER = {
    "role": "user",
    "content": ("Your interruptions list has more entries than there are (INTERRUPTION)-marked "
                "lines in the transcript. Return the full JSON again, including only entries for "
                "lines actually marked (INTERRUPTION)."),
}


def _sanitize_interruption_count(data: dict, marked_count: int) -> dict:
    """Regex-retry didn't fix it, so fall back to keeping only the first
    marked_count entries — the model tends to discuss interruptions in the
    order they occurred, so the earliest entries are the most likely to be
    the real ones. Everything past that is dropped."""
    sanitized = copy.deepcopy(data)
    sanitized["interruptions"] = sanitized.get("interruptions", [])[:marked_count]
    return sanitized


def call_judge(turns: list[dict], model: str = JUDGE_MODEL,
                sentiment_results: Optional[list[dict]] = None,
                counterpart_description: Optional[str] = None) -> dict:
    if _no_user_speech(turns):
        return _zero_response_result()
    annotated = compute_timing_signals(turns)
    messages = build_judge_prompt(annotated, sentiment_results, counterpart_description)
    allowed_numbers = _extract_numbers(_dialogue_text(turns))
    marked_count = len(interruption_turns(annotated))

    data = None
    last_error: Optional[Exception] = None
    attempt_messages = messages
    for _ in range(_MAX_JSON_ATTEMPTS):
        try:
            data = _request_judge_json(attempt_messages, model)
            break
        except ValueError as err:
            last_error = err
            attempt_messages = messages + [_INVALID_JSON_REMINDER]
    if data is None:
        raise ValueError(
            f"Judge did not return valid output after {_MAX_JSON_ATTEMPTS} attempts: {last_error}"
        )

    if _invented_numbers_in_examples(data, allowed_numbers):
        try:
            retried = _request_judge_json(messages + [_NUMBER_RETRY_REMINDER], model)
        except ValueError:
            retried = None
        if retried is not None and not _invented_numbers_in_examples(retried, allowed_numbers):
            data = retried
        else:
            data = _sanitize_all_examples(retried or data, allowed_numbers)

    if len(data.get("interruptions", [])) > marked_count:
        try:
            retried = _request_judge_json(messages + [_INTERRUPTION_COUNT_RETRY_REMINDER], model)
        except ValueError:
            retried = None
        if retried is not None and len(retried.get("interruptions", [])) <= marked_count:
            data = retried
        else:
            data = _sanitize_interruption_count(retried or data, marked_count)

    return data


def _wait_for_artifact(session_id: str, artifact_type: str, timeout: float = 20.0,
                        poll_interval: float = 1.5) -> tuple[dict, Optional[str]]:
    """AssemblyAI can take a few seconds to finalize and upload session
    artifacts after the call ends, so a request made right on session.ended
    may not find them yet. Polls until the artifact shows up or timeout
    elapses; returns the last session fetched either way."""
    deadline = time.monotonic() + timeout
    session = fetch_session(session_id)
    url = _artifact_url(session, artifact_type)
    while url is None and time.monotonic() < deadline:
        time.sleep(poll_interval)
        session = fetch_session(session_id)
        url = _artifact_url(session, artifact_type)
    return session, url


def _counterpart_description(session: dict) -> Optional[str]:
    """The first sentence of the agent's own system prompt — by convention
    every persona's prompt opens with "You are <Name>, a <role>..." — tells
    the judge who the founder was actually talking to, without needing a
    separate persona registry here that would drift out of sync as personas
    are added (see agents/*.jsonc)."""
    prompt = session.get("config", {}).get("system_prompt", "")
    first_sentence = prompt.split(".", 1)[0].strip()
    return f"{first_sentence}." if first_sentence else None


def run_judge_pass(session_id: str, model: str = JUDGE_MODEL, include_sentiment: bool = True) -> dict:
    """The single entry point the UI calls: a session id in, validated judge
    JSON out. Sentiment analysis is best-effort — a failure there (gateway
    timeout, no account access) still lets the judge pass complete."""
    session, timeline_url = _wait_for_artifact(session_id, "timeline")
    if timeline_url is None:
        raise ValueError(f"Session {session_id} has no timeline artifact yet (status={session.get('status')})")
    with urllib.request.urlopen(timeline_url) as res:
        timeline = json.loads(res.read().decode())
    turns = parse_timeline(timeline)
    if _no_user_speech(turns):
        # Skip sentiment analysis too, not just the LLM call — there's
        # nothing for either to add when the founder never spoke.
        return _zero_response_result()
    counterpart_description = _counterpart_description(session)

    sentiment_results = None
    if include_sentiment:
        audio_url = _artifact_url(session, "audio")
        if audio_url is not None:
            try:
                sentiment_results = fetch_sentiment(audio_url)
            except Exception as err:
                print(f"Warning: sentiment analysis failed, continuing without it: {err}")

    return call_judge(turns, model=model, sentiment_results=sentiment_results,
                       counterpart_description=counterpart_description)


# --- schema validation ---------------------------------------------------------

_TRIGGERS = {"vague_claim", "unsupported_number", "hesitation", "ignored_question"}
_RECOVERY_QUALITIES = {"strong", "weak", "poor"}
_RECOVERY_PATTERNS = {"answered_directly", "deflected", "repeated_claim", "asked_clarifying_question"}
_CATEGORY_KEYS = ("content_substance", "composure_under_pressure", "audience_responsiveness")


def _validate_score(value: Any, label: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"'{label}' must be a number, got {type(value).__name__}")
    if not (0 <= value <= 100):
        raise ValueError(f"'{label}' must be 0-100, got {value}")


def validate_judge_output(data: Any) -> None:
    """Raises ValueError describing the first problem found. Returns None on success."""
    if not isinstance(data, dict):
        raise ValueError(f"Judge output must be a JSON object, got {type(data).__name__}")

    if "overall_score" not in data:
        raise ValueError("Judge output missing 'overall_score'")
    _validate_score(data["overall_score"], "overall_score")

    if "categories" not in data:
        raise ValueError("Judge output missing 'categories'")
    categories = data["categories"]
    if not isinstance(categories, dict):
        raise ValueError(f"'categories' must be an object, got {type(categories).__name__}")
    for key in _CATEGORY_KEYS:
        if key not in categories:
            raise ValueError(f"'categories' missing '{key}'")
        entry = categories[key]
        if not isinstance(entry, dict):
            raise ValueError(f"categories.{key} must be an object, got {type(entry).__name__}")
        if "score" not in entry:
            raise ValueError(f"categories.{key} missing 'score'")
        _validate_score(entry["score"], f"categories.{key}.score")
        if not isinstance(entry.get("note"), str) or not entry["note"].strip():
            raise ValueError(f"categories.{key}.note must be a non-empty string")

    if "interruptions" not in data:
        raise ValueError("Judge output missing 'interruptions'")
    interruptions = data["interruptions"]
    if not isinstance(interruptions, list):
        raise ValueError(f"'interruptions' must be a list, got {type(interruptions).__name__}")
    for i, item in enumerate(interruptions):
        if not isinstance(item, dict):
            raise ValueError(f"interruptions[{i}] must be an object, got {type(item).__name__}")
        for field in ("moment", "trigger", "recovery_quality", "recovery_pattern", "note", "better_response_example"):
            if field not in item:
                raise ValueError(f"interruptions[{i}] missing '{field}'")
        if item["trigger"] not in _TRIGGERS:
            raise ValueError(
                f"interruptions[{i}].trigger must be one of {sorted(_TRIGGERS)}, got {item['trigger']!r}"
            )
        if item["recovery_quality"] not in _RECOVERY_QUALITIES:
            raise ValueError(
                f"interruptions[{i}].recovery_quality must be one of "
                f"{sorted(_RECOVERY_QUALITIES)}, got {item['recovery_quality']!r}"
            )
        if item["recovery_pattern"] not in _RECOVERY_PATTERNS:
            raise ValueError(
                f"interruptions[{i}].recovery_pattern must be one of "
                f"{sorted(_RECOVERY_PATTERNS)}, got {item['recovery_pattern']!r}"
            )
        for field in ("moment", "note", "better_response_example"):
            if not isinstance(item[field], str) or not item[field].strip():
                raise ValueError(f"interruptions[{i}].{field} must be a non-empty string")

    if "suggestions" not in data:
        raise ValueError("Judge output missing 'suggestions'")
    suggestions = data["suggestions"]
    if not isinstance(suggestions, list) or not suggestions:
        raise ValueError("'suggestions' must be a non-empty list")
    for i, s in enumerate(suggestions):
        if not isinstance(s, str) or not s.strip():
            raise ValueError(f"suggestions[{i}] must be a non-empty string")
