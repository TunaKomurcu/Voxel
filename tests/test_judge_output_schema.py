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
    "interruptions": [
        {
            "moment": "vague market size claim",
            "recovery_quality": "weak",
            "note": "Restated the same number instead of citing a source.",
        }
    ],
    "suggestions": [
        "Bring one real customer number.",
        "Answer the unit economics question directly.",
    ],
}

GREETING_TURN = {"trigger": "greeting", "user_transcript": None, "agent_text": "Hi", "is_interruption": False}


def test_valid_output_passes():
    judge.validate_judge_output(VALID)  # must not raise


@pytest.mark.parametrize(
    "mutate, message_fragment",
    [
        (lambda d: d.pop("overall_score"), "overall_score"),
        (lambda d: d.__setitem__("overall_score", 150), "0-100"),
        (lambda d: d.__setitem__("overall_score", "high"), "must be a number"),
        (lambda d: d.pop("interruptions"), "interruptions"),
        (lambda d: d.__setitem__("interruptions", "not a list"), "must be a list"),
        (lambda d: d["interruptions"][0].__setitem__("recovery_quality", "meh"), "recovery_quality"),
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
        result = judge.call_judge([GREETING_TURN])
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
        result = judge.call_judge([GREETING_TURN])
    assert result == VALID


def test_call_judge_raises_on_schema_violation():
    broken = copy.deepcopy(VALID)
    broken["overall_score"] = 999
    fake_response = {"choices": [{"message": {"content": json.dumps(broken)}}]}
    with patch("lib.llm_gateway_chat", return_value=fake_response):
        with pytest.raises(ValueError, match="0-100"):
            judge.call_judge([GREETING_TURN])
