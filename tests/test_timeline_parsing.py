import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import judge

FIXTURE = Path(__file__).parent / "fixtures" / "sample_session_timeline.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_parses_all_turns():
    timeline = load_fixture()
    turns = judge.parse_timeline(timeline)
    assert len(turns) == len(timeline["turns"])


def test_greeting_turn_is_not_an_interruption():
    turns = judge.parse_timeline(load_fixture())
    assert turns[0]["trigger"] == "greeting"
    assert turns[0]["is_interruption"] is False


def test_flags_the_known_interruption():
    turns = judge.parse_timeline(load_fixture())
    interruptions = judge.interruption_turns(turns)
    assert len(interruptions) == 1
    assert "Uber of pet care" in interruptions[0]["user_transcript"]
    assert interruptions[0]["agent_text"].startswith("Cut the analogies")


def test_timing_based_signal_matches_real_barge_in():
    # Ground truth from the live session: the agent's reply started before
    # AssemblyAI marked the user's speech as ended.
    turns = judge.parse_timeline(load_fixture())
    interrupted = turns[1]
    assert interrupted["agent_reply_started_at_ms"] <= interrupted["user_speech_ended_at_ms"]


def test_text_fallback_used_when_timing_missing():
    turn = {
        "trigger": "user_speech",
        "user_transcript": "so our growth rate is basically",
        "agent_reply_started_at_ms": None,
        "user_speech_ended_at_ms": None,
        "agent_text": "Basically what number?",
    }
    assert judge._is_interruption(turn) is True


def test_complete_sentence_without_timing_is_not_flagged():
    turn = {
        "trigger": "user_speech",
        "user_transcript": "We charge a flat ten percent transaction fee.",
        "agent_reply_started_at_ms": None,
        "user_speech_ended_at_ms": None,
        "agent_text": "Good, who are your first paying customers?",
    }
    assert judge._is_interruption(turn) is False
