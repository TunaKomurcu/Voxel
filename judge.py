"""Session retrieval, interruption detection, and the judge pass.

Standard library only, matching lib.py's "no pip install" rule.

Session retrieval: GET /sessions/{id} does not inline the timeline — it
points to it via a presigned S3 URL in artifacts[type=="timeline"], fetched
separately in fetch_timeline().
"""

import json
import re
import urllib.request
from typing import Any, Optional

import lib

# TEMPORARY: this account doesn't have LLM Gateway access to any Claude model
# yet ("Your account does not have access to this LLM Gateway model" on both
# claude-sonnet-5 and claude-haiku-4-5-20251001, confirmed live against
# /v1/chat/completions). Switch back to "claude-sonnet-5" once that's enabled
# on the AssemblyAI dashboard — see CLAUDE.md's LLM Gateway decision.
JUDGE_MODEL = "qwen3.5-4b-32k-fast"

JUDGE_SYSTEM_PROMPT = """You are an objective evaluation judge for a pitch-practice call between a founder and a skeptical investor persona. You did not take part in the call; you are reading a transcript after the fact.

For each moment marked (INTERRUPTION) below, judge how well the founder recovered in their next turn: did they answer the investor's sharp follow-up specifically, or did they stay vague, dodge, or restate the same hand-wavy claim?

Return ONLY valid JSON, no prose before or after, matching exactly this shape:
{
  "overall_score": <integer 0-100>,
  "interruptions": [
    {"moment": "<short description of what triggered the interruption>",
     "recovery_quality": "strong" | "weak" | "poor",
     "note": "<one concrete sentence about what they said or should have said>"}
  ],
  "suggestions": ["<actionable suggestion>", "..."]
}

If there were no interruptions in the transcript, base overall_score on general pitch clarity, return an empty "interruptions" list, and still give 1-3 general suggestions."""


# --- session + timeline ------------------------------------------------------


def fetch_session(session_id: str) -> dict:
    return lib.aai(f"/sessions/{session_id}")


def fetch_timeline(session_id: str) -> dict:
    session = fetch_session(session_id)
    artifact = next((a for a in session.get("artifacts", []) if a.get("type") == "timeline"), None)
    if artifact is None:
        raise ValueError(f"Session {session_id} has no timeline artifact yet (status={session.get('status')})")
    with urllib.request.urlopen(artifact["url"]) as res:
        return json.loads(res.read().decode())


def _is_interruption(turn: dict) -> bool:
    """Primary signal: the agent's reply started at or before the moment
    AssemblyAI's own turn-detection marked the user's speech as ended — a
    real barge-in, confirmed against a live session. Falls back to a text
    heuristic (unpunctuated, trailing transcript) when timing is missing."""
    if turn.get("trigger") != "user_speech":
        return False
    started = turn.get("agent_reply_started_at_ms")
    ended = turn.get("user_speech_ended_at_ms")
    if started is not None and ended is not None:
        return started <= ended
    transcript = (turn.get("user_transcript") or "").strip()
    return bool(transcript) and transcript[-1] not in ".?!"


def parse_timeline(timeline: dict) -> list[dict]:
    return [{**turn, "is_interruption": _is_interruption(turn)} for turn in timeline.get("turns", [])]


def interruption_turns(turns: list[dict]) -> list[dict]:
    return [t for t in turns if t["is_interruption"]]


# --- judge pass ----------------------------------------------------------------


def build_judge_prompt(turns: list[dict]) -> list[dict]:
    lines = []
    for turn in turns:
        if turn.get("user_transcript"):
            lines.append(f"[Founder]: {turn['user_transcript']}")
        marker = " (INTERRUPTION)" if turn.get("is_interruption") else ""
        lines.append(f"[Investor{marker}]: {turn['agent_text']}")
    transcript_text = "\n".join(lines)
    return [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": f"Transcript:\n\n{transcript_text}"},
    ]


_CODE_FENCE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _strip_code_fence(content: str) -> str:
    """Some gateway models wrap JSON in a markdown fence despite instructions
    not to. Strip it if present; leave anything else untouched."""
    match = _CODE_FENCE.match(content)
    return match.group(1) if match else content


def call_judge(turns: list[dict], model: str = JUDGE_MODEL) -> dict:
    messages = build_judge_prompt(turns)
    response = lib.llm_gateway_chat(model=model, messages=messages, max_tokens=1024)
    content = response["choices"][0]["message"]["content"]
    try:
        data = json.loads(_strip_code_fence(content))
    except json.JSONDecodeError as err:
        raise ValueError(f"Judge did not return valid JSON: {content!r}") from err
    validate_judge_output(data)
    return data


# --- schema validation ---------------------------------------------------------

_RECOVERY_QUALITIES = {"strong", "weak", "poor"}


def validate_judge_output(data: Any) -> None:
    """Raises ValueError describing the first problem found. Returns None on success."""
    if not isinstance(data, dict):
        raise ValueError(f"Judge output must be a JSON object, got {type(data).__name__}")

    if "overall_score" not in data:
        raise ValueError("Judge output missing 'overall_score'")
    score = data["overall_score"]
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        raise ValueError(f"'overall_score' must be a number, got {type(score).__name__}")
    if not (0 <= score <= 100):
        raise ValueError(f"'overall_score' must be 0-100, got {score}")

    if "interruptions" not in data:
        raise ValueError("Judge output missing 'interruptions'")
    interruptions = data["interruptions"]
    if not isinstance(interruptions, list):
        raise ValueError(f"'interruptions' must be a list, got {type(interruptions).__name__}")
    for i, item in enumerate(interruptions):
        if not isinstance(item, dict):
            raise ValueError(f"interruptions[{i}] must be an object, got {type(item).__name__}")
        for field in ("moment", "recovery_quality", "note"):
            if field not in item:
                raise ValueError(f"interruptions[{i}] missing '{field}'")
        if item["recovery_quality"] not in _RECOVERY_QUALITIES:
            raise ValueError(
                f"interruptions[{i}].recovery_quality must be one of "
                f"{sorted(_RECOVERY_QUALITIES)}, got {item['recovery_quality']!r}"
            )
        if not isinstance(item["moment"], str) or not item["moment"].strip():
            raise ValueError(f"interruptions[{i}].moment must be a non-empty string")
        if not isinstance(item["note"], str) or not item["note"].strip():
            raise ValueError(f"interruptions[{i}].note must be a non-empty string")

    if "suggestions" not in data:
        raise ValueError("Judge output missing 'suggestions'")
    suggestions = data["suggestions"]
    if not isinstance(suggestions, list) or not suggestions:
        raise ValueError("'suggestions' must be a non-empty list")
    for i, s in enumerate(suggestions):
        if not isinstance(s, str) or not s.strip():
            raise ValueError(f"suggestions[{i}] must be a non-empty string")
