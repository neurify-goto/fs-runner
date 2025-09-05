import json
import builtins
import pytest

from src.form_sender.utils.error_classifier import ErrorClassifier


def test_external_config_malformed(monkeypatch):
    # Reset loader flags
    ErrorClassifier._external_rules_loaded = False
    ErrorClassifier._external_extra_patterns = {}

    # Force exists
    def fake_exists(path):
        return True

    # json.load raises JSONDecodeError
    class DummyFile:
        def __enter__(self):
            return self
        def __exit__(self, *args, **kwargs):
            return False
        def read(self):
            return "invalid-json"

    def fake_open(*args, **kwargs):
        return DummyFile()

    def fake_json_load(_):
        raise json.JSONDecodeError("Invalid", "invalid", 0)

    monkeypatch.setattr("os.path.exists", fake_exists)
    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(json, "load", fake_json_load)

    # Should not raise; fallback to internal rules
    detail = ErrorClassifier.classify_detail(error_message="Timeout 30000ms exceeded")
    assert isinstance(detail, dict)


@pytest.mark.parametrize(
    "msg,expected_min",
    [
        ("", 0.2),  # no evidence -> at least MIN_CONFIDENCE
        ("DNS lookup failed", 0.6),  # strong evidence should push higher
    ],
)
def test_calculate_confidence_edges(msg, expected_min):
    detail = ErrorClassifier.classify_detail(error_message=msg)
    assert detail["confidence"] >= expected_min - 1e-6

