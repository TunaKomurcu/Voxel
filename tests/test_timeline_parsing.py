import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import judge
import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "sample_session_timeline.json"
NO_INTERRUPTION_FIXTURE = Path(__file__).parent / "fixtures" / "no_interruption_session.json"
HESITATION_FIXTURE = Path(__file__).parent / "fixtures" / "hesitation_session.json"


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
    assert interruptions[0]["interruption_type"] == "barge_in"


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


def test_no_interruption_fixture_flags_nothing_despite_a_truncated_agent_turn():
    # Real session: the founder was never cut off, but the agent's OWN reply
    # got cut off by the user on one turn (status "interrupted", agent_text
    # a stray fragment "If") — the reverse direction from what we detect.
    # That must not get flagged as our kind of interruption, and must not
    # feed into any timing calculation (compute_timing_signals only touches
    # turns with is_interruption=True, and none exist here).
    timeline = json.loads(NO_INTERRUPTION_FIXTURE.read_text(encoding="utf-8"))
    turns = judge.parse_timeline(timeline)
    assert len(turns) == 5
    assert all(t["is_interruption"] is False for t in turns)

    truncated = turns[2]
    assert truncated["status"] == "interrupted"
    assert truncated["agent_text"] == "If"

    annotated = judge.compute_timing_signals(turns)
    assert all("response_latency_ms" not in t for t in annotated)


def test_hesitation_fixture_flags_both_trailed_off_turns_as_hesitation_cutoff():
    # Real session, deliberately paused mid-sentence twice ("So our monthly
    # growth rate is" / "So it's based on"). Neither is a barge-in — the
    # agent starts 800ms-1.5s AFTER user_speech_ended_at_ms in both cases,
    # same range as a normal, un-interrupted reply elsewhere (see
    # no_interruption_session.json) — so only the text signal catches these.
    timeline = json.loads(HESITATION_FIXTURE.read_text(encoding="utf-8"))
    turns = judge.parse_timeline(timeline)
    interruptions = judge.interruption_turns(turns)
    assert len(interruptions) == 2
    assert all(t["interruption_type"] == "hesitation_cutoff" for t in interruptions)
    assert {t["user_transcript"] for t in interruptions} == {
        "So our monthly growth rate is",
        "So it's based on",
    }


def test_hesitation_fixture_does_not_flag_the_short_punctuated_answer():
    # "55%." is a complete, punctuated answer with null timestamps (a
    # malformed record in the real timeline) — must not crash and must not
    # be flagged via either signal.
    timeline = json.loads(HESITATION_FIXTURE.read_text(encoding="utf-8"))
    turns = judge.parse_timeline(timeline)
    short_answer = next(t for t in turns if t.get("user_transcript") == "55%.")
    assert short_answer["is_interruption"] is False
    assert short_answer["interruption_type"] is None


def test_hesitation_fixture_ignores_the_mislabeled_mid_call_greeting_turn():
    # One agent turn mid-call carries trigger "greeting" (an AssemblyAI
    # timeline labeling quirk, not a real session restart) with a null
    # agent_reply_started_at_ms. Must not crash, must not be flagged.
    timeline = json.loads(HESITATION_FIXTURE.read_text(encoding="utf-8"))
    turns = judge.parse_timeline(timeline)
    stray_greeting = next(
        t for t in turns if t["trigger"] == "greeting" and t.get("agent_text", "").startswith("Is that based on")
    )
    assert stray_greeting["is_interruption"] is False
    assert stray_greeting["interruption_type"] is None


def test_build_judge_prompt_skips_the_null_agent_text_turn():
    # hesitation_session.json's malformed "55%." turn has agent_text=None.
    # build_judge_prompt must not print a literal "[Investor]: None" line.
    timeline = json.loads(HESITATION_FIXTURE.read_text(encoding="utf-8"))
    turns = judge.parse_timeline(timeline)
    messages = judge.build_judge_prompt(judge.compute_timing_signals(turns))
    assert "None" not in messages[1]["content"]


@pytest.mark.parametrize(
    "transcript, expected_type",
    [
        ("so our growth rate is basically", "hesitation_cutoff"),
        ("We charge a flat ten percent transaction fee.", None),
        ("Yes.", None),
        ("No!", None),
        ("What do you mean?", None),
    ],
)
def test_interruption_type_from_text_alone(transcript, expected_type):
    # No timing data at all, so only _is_hesitation_cutoff can fire.
    turn = {"trigger": "user_speech", "user_transcript": transcript,
            "agent_reply_started_at_ms": None, "user_speech_ended_at_ms": None}
    assert judge._interruption_type(turn) == expected_type


def test_barge_in_takes_priority_over_hesitation_text_when_both_signals_present():
    # A trailing-off transcript that ALSO overlaps in time with the agent's
    # reply is a barge-in, not a hesitation cutoff — timing wins when both
    # signals could apply.
    turn = {
        "trigger": "user_speech",
        "user_transcript": "so our growth rate is",  # would read as hesitation_cutoff alone
        "user_speech_ended_at_ms": 5000,
        "agent_reply_started_at_ms": 4800,  # started before the user finished
    }
    assert judge._interruption_type(turn) == "barge_in"
