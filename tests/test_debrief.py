import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import debrief
import judge

EVALUATION = {
    "overall_score": 72,
    "categories": {
        "content_substance": {"score": 60, "note": "Named a market but never sized it."},
        "composure_under_pressure": {"score": 70, "note": "Recovered tone quickly after the cut-in."},
        "audience_responsiveness": {"score": 80, "note": "Picked up on the investor's follow-up question."},
    },
    "interruptions": [
        {"recovery_quality": "strong"},
        {"recovery_quality": "weak"},
    ],
    "suggestions": ["Lead with one sourced number", "Answer before elaborating"],
}


def test_includes_score_weakest_strongest_tip_and_recovery():
    text = debrief.build_debrief_text(EVALUATION, "Marcus")
    assert text.startswith("Marcus here. You scored 72 out of 100.")
    assert "Work on content substance: Named a market but never sized it." in text
    assert "Strongest was audience responsiveness" in text
    assert "Next time: Lead with one sourced number." in text
    assert "You recovered well from 1 of 2 interruptions." in text
    assert len(text.split()) <= debrief.MAX_WORDS


def test_drops_lowest_priority_pieces_whole_to_fit_the_cap():
    long_note = "word " * 30
    evaluation = {**EVALUATION, "categories": {
        **EVALUATION["categories"],
        "audience_responsiveness": {"score": 80, "note": long_note},
    }}
    text = debrief.build_debrief_text(evaluation, "Marcus")
    assert len(text.split()) <= debrief.MAX_WORDS
    assert "Strongest was" not in text
    assert "Work on content substance" in text


def test_zero_response_result_has_no_strongest_area():
    text = debrief.build_debrief_text(judge._zero_response_result())
    assert text.startswith("You scored 0 out of 100.")
    assert "Strongest" not in text
    assert "No response was given." in text
    assert "interruptions" not in text


def test_unknown_persona_falls_back_to_default_voice():
    assert debrief.voice_for("investor") == debrief.DEBRIEF_VOICES["investor"]
    assert debrief.voice_for("default") == debrief.DEFAULT_VOICE
    assert debrief.voice_for(None) == debrief.DEFAULT_VOICE


def test_disabled_without_api_key(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert debrief.is_enabled() is False
