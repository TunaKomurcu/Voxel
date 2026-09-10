import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import judge
import pytest

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


def test_compute_timing_signals_on_real_fixture_last_turn_has_no_recovery():
    # The real session's interruption is also its last turn, so there's no
    # recovery utterance to measure response_latency_ms or recovery_wps against.
    turns = judge.parse_timeline(load_fixture())
    annotated = judge.compute_timing_signals(turns)
    interrupted = annotated[1]
    assert interrupted["pre_interrupt_wps"] == pytest.approx(2.76, abs=0.01)
    assert interrupted["response_latency_ms"] is None
    assert interrupted["recovery_wps"] is None


def test_compute_timing_signals_with_a_following_recovery_turn():
    turns = [
        {
            "trigger": "user_speech",
            "user_transcript": "we are the uber of pet care",
            "user_speech_started_at_ms": 1000,
            "user_speech_ended_at_ms": 3000,
            "agent_reply_started_at_ms": 2500,
            "agent_reply_ended_at_ms": 4000,
            "agent_text": "Cut the analogies, what's your unit economics?",
            "is_interruption": True,
        },
        {
            "trigger": "user_speech",
            "user_transcript": "we charge ten percent per booking",
            "user_speech_started_at_ms": 4500,
            "user_speech_ended_at_ms": 6000,
            "agent_reply_started_at_ms": 6200,
            "agent_reply_ended_at_ms": 7000,
            "agent_text": "Good, keep going.",
            "is_interruption": False,
        },
    ]
    annotated = judge.compute_timing_signals(turns)
    interrupted = annotated[0]
    assert interrupted["response_latency_ms"] == 500  # recovery started 4500, interruption ended 4000
    assert interrupted["pre_interrupt_wps"] == pytest.approx(3.5, abs=0.01)  # 7 words / 2s
    assert interrupted["recovery_wps"] == pytest.approx(4.0, abs=0.01)  # 6 words / 1.5s
