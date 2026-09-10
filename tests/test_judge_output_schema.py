import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import judge
import pytest

VALID = {
    "overall_score": 72,
    "categories": {
        "content_substance": {"score": 60, "note": "Named a market but never sized it."},
        "composure_under_pressure": {"score": 70, "note": "Recovered tone quickly after the cut-in."},
        "audience_responsiveness": {"score": 80, "note": "Picked up on the investor's follow-up question."},
    },
    "interruptions": [
        {
            "moment": "vague market size claim",
            "trigger": "vague_claim",
            "recovery_quality": "weak",
            "recovery_pattern": "repeated_claim",
            "note": "Restated the same number instead of citing a source.",
            "better_response_example": "Instead of 'huge market', say '2,400 vets in our target metro, each worth $3k/year in tool spend'.",
        }
    ],
    "suggestions": [
        "Bring one real customer number.",
        "Answer the unit economics question directly.",
    ],
}

GREETING_TURN = {"trigger": "greeting", "user_transcript": None, "agent_text": "Hi", "is_interruption": False}

# VALID's better_response_example cites "2,400 vets" and "$3k" — these turns
# actually contain those numbers, so the invented-number check in call_judge
# doesn't trigger an extra retry and these tests can assert a single call.
TURNS_WITH_MATCHING_NUMBERS = [
    {
        "trigger": "user_speech",
        "user_transcript": "We have 2,400 vets in our target metro, each worth $3k a year in tool spend.",
        "agent_text": "Good, keep going.",
        "is_interruption": False,
    }
]


def test_valid_output_passes():
    judge.validate_judge_output(VALID)  # must not raise


@pytest.mark.parametrize(
    "mutate, message_fragment",
    [
        (lambda d: d.pop("overall_score"), "overall_score"),
        (lambda d: d.__setitem__("overall_score", 150), "0-100"),
        (lambda d: d.__setitem__("overall_score", "high"), "must be a number"),
        (lambda d: d.pop("categories"), "categories"),
        (lambda d: d["categories"].pop("composure_under_pressure"), "composure_under_pressure"),
        (lambda d: d["categories"]["content_substance"].__setitem__("score", 101), "0-100"),
        (lambda d: d["categories"]["content_substance"].pop("note"), "note"),
        (lambda d: d.pop("interruptions"), "interruptions"),
        (lambda d: d.__setitem__("interruptions", "not a list"), "must be a list"),
        (lambda d: d["interruptions"][0].__setitem__("trigger", "founder_was_rude"), "trigger"),
        (lambda d: d["interruptions"][0].pop("trigger"), "trigger"),
        (lambda d: d["interruptions"][0].__setitem__("recovery_quality", "meh"), "recovery_quality"),
        (lambda d: d["interruptions"][0].__setitem__("recovery_pattern", "shrugged"), "recovery_pattern"),
        (lambda d: d["interruptions"][0].pop("recovery_pattern"), "recovery_pattern"),
        (lambda d: d["interruptions"][0].pop("better_response_example"), "better_response_example"),
        (lambda d: d["interruptions"][0].__setitem__("better_response_example", "  "), "better_response_example"),
        (lambda d: d["interruptions"][0].pop("note"), "note"),
        (lambda d: d.pop("suggestions"), "suggestions"),
        (lambda d: d.__setitem__("suggestions", []), "non-empty list"),
    ],
)
def test_rejects_malformed_output(mutate, message_fragment):
    data = copy.deepcopy(VALID)
    mutate(data)
    with pytest.raises(ValueError, match=message_fragment):
        judge.validate_judge_output(data)


def test_call_judge_with_mocked_gateway():
    fake_response = {"choices": [{"message": {"content": json.dumps(VALID)}}]}
    with patch("lib.llm_gateway_chat", return_value=fake_response) as mocked:
        result = judge.call_judge(TURNS_WITH_MATCHING_NUMBERS)
    assert result == VALID
    mocked.assert_called_once()


def test_call_judge_raises_on_non_json_content():
    fake_response = {"choices": [{"message": {"content": "not json"}}]}
    with patch("lib.llm_gateway_chat", return_value=fake_response):
        with pytest.raises(ValueError):
            judge.call_judge([GREETING_TURN])


def test_call_judge_strips_markdown_code_fence():
    # Some gateway models wrap the JSON in ```json ... ``` despite instructions not to.
    fenced = "```json\n" + json.dumps(VALID) + "\n```"
    fake_response = {"choices": [{"message": {"content": fenced}}]}
    with patch("lib.llm_gateway_chat", return_value=fake_response):
        result = judge.call_judge(TURNS_WITH_MATCHING_NUMBERS)
    assert result == VALID


def test_call_judge_raises_on_schema_violation():
    broken = copy.deepcopy(VALID)
    broken["overall_score"] = 999
    fake_response = {"choices": [{"message": {"content": json.dumps(broken)}}]}
    with patch("lib.llm_gateway_chat", return_value=fake_response):
        with pytest.raises(ValueError, match="0-100"):
            judge.call_judge([GREETING_TURN])


def _response(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


# --- JSON retry ---------------------------------------------------------------


def test_json_retry_succeeds_on_a_later_attempt():
    responses = [_response("not json"), _response("also not json"), _response(json.dumps(VALID))]
    with patch("lib.llm_gateway_chat", side_effect=responses) as mocked:
        result = judge.call_judge(TURNS_WITH_MATCHING_NUMBERS)
    assert result == VALID
    assert mocked.call_count == 3


def test_json_retry_gives_up_after_max_attempts():
    with patch("lib.llm_gateway_chat", return_value=_response("not json")) as mocked:
        with pytest.raises(ValueError, match="3 attempts"):
            judge.call_judge(TURNS_WITH_MATCHING_NUMBERS)
    assert mocked.call_count == 3


# --- invented-number retry and sanitize fallback -------------------------------


def _with_example(text: str) -> dict:
    data = copy.deepcopy(VALID)
    data["interruptions"][0]["better_response_example"] = text
    return data


def test_invented_number_triggers_one_retry_and_clean_retry_is_used():
    invented = _with_example("We hit an LTV of $45 and a CAC of $12.")  # not in GREETING_TURN's dialogue
    clean = _with_example("We hit an LTV of $[X] and a CAC of $[Y].")
    responses = [_response(json.dumps(invented)), _response(json.dumps(clean))]
    with patch("lib.llm_gateway_chat", side_effect=responses) as mocked:
        result = judge.call_judge([GREETING_TURN])
    assert result["interruptions"][0]["better_response_example"] == "We hit an LTV of $[X] and a CAC of $[Y]."
    assert mocked.call_count == 2


def test_invented_number_falls_back_to_sanitize_when_retry_still_bad():
    invented = _with_example("We hit an LTV of $45 and a CAC of $12.")
    still_invented = _with_example("Our LTV is $99 per customer.")
    responses = [_response(json.dumps(invented)), _response(json.dumps(still_invented))]
    with patch("lib.llm_gateway_chat", side_effect=responses) as mocked:
        result = judge.call_judge([GREETING_TURN])
    example = result["interruptions"][0]["better_response_example"]
    assert "99" not in example
    assert "[X]" in example
    assert mocked.call_count == 2


def test_real_number_in_transcript_is_not_flagged():
    # TURNS_WITH_MATCHING_NUMBERS' dialogue actually contains 2,400 and $3k,
    # so VALID's better_response_example (which cites them) needs no retry.
    with patch("lib.llm_gateway_chat", return_value=_response(json.dumps(VALID))) as mocked:
        result = judge.call_judge(TURNS_WITH_MATCHING_NUMBERS)
    assert result == VALID
    mocked.assert_called_once()


# --- helper functions -----------------------------------------------------------


def test_extract_numbers_normalizes_currency_percent_and_commas():
    numbers = judge._extract_numbers("We charge $3k on 2,400 accounts, a 12% take rate.")
    assert numbers == {"3", "2400", "12"}


def test_sanitize_response_example_replaces_unknown_numbers_only():
    text = "We hit an LTV of $45 and our real CAC of $12."
    sanitized = judge.sanitize_response_example(text, allowed_numbers={"12"})
    assert sanitized == "We hit an LTV of $[X] and our real CAC of $12."


def test_sanitize_response_example_cycles_placeholder_letters():
    text = "$45 and $99 and $150"
    sanitized = judge.sanitize_response_example(text, allowed_numbers=set())
    assert sanitized == "$[X] and $[Y] and $[Z]"
