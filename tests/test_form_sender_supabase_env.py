import base64
import importlib
import json
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1] / "src"


def _import_runner(monkeypatch, env=None):
    env = env or {}
    for var in (
        "FORM_SENDER_TABLE_MODE",
        "FORM_SENDER_TEST_MODE",
        "JOB_EXECUTION_META",
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_URL_TEST",
        "SUPABASE_SERVICE_ROLE_KEY_TEST",
    ):
        monkeypatch.delenv(var, raising=False)

    for key, value in env.items():
        monkeypatch.setenv(key, value)

    if "form_sender_runner" in sys.modules:
        del sys.modules["form_sender_runner"]

    monkeypatch.syspath_prepend(str(SRC_DIR))
    return importlib.import_module("form_sender_runner")


def _capture_client(monkeypatch, module):
    captured = {}

    def fake_create_client(url, key):
        captured["url"] = url
        captured["key"] = key
        return object()

    monkeypatch.setattr(module, "create_client", fake_create_client)
    return captured


def test_build_supabase_client_uses_test_credentials(monkeypatch):
    module = _import_runner(
        monkeypatch,
        {
            "FORM_SENDER_TEST_MODE": "1",
            "SUPABASE_URL_TEST": "https://example.test.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY_TEST": "test-key",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "prod-key",
        },
    )

    captured = _capture_client(monkeypatch, module)
    module._build_supabase_client()

    assert captured["url"] == "https://example.test.supabase.co"
    assert captured["key"] == "test-key"


def test_build_supabase_client_requires_test_credentials_in_test_mode(monkeypatch):
    module = _import_runner(
        monkeypatch,
        {
            "FORM_SENDER_TEST_MODE": "true",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "prod-key",
        },
    )

    with pytest.raises(RuntimeError) as exc:
        module._build_supabase_client()

    assert "SUPABASE_URL_TEST" in str(exc.value)


def test_build_supabase_client_uses_production_when_not_test(monkeypatch):
    module = _import_runner(
        monkeypatch,
        {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "prod-key",
        },
    )

    captured = _capture_client(monkeypatch, module)
    module._build_supabase_client()

    assert captured["url"] == "https://example.supabase.co"
    assert captured["key"] == "prod-key"


def test_build_supabase_client_respects_table_mode_test(monkeypatch):
    module = _import_runner(
        monkeypatch,
        {
            "FORM_SENDER_TABLE_MODE": "test",
            "SUPABASE_URL_TEST": "https://example.test.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY_TEST": "test-key",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "prod-key",
        },
    )

    captured = _capture_client(monkeypatch, module)
    module._build_supabase_client()

    assert captured["url"] == "https://example.test.supabase.co"
    assert captured["key"] == "test-key"


def test_build_supabase_client_respects_meta_test_flag(monkeypatch):
    meta = base64.b64encode(json.dumps({"test_mode": True}).encode("utf-8")).decode("utf-8")
    module = _import_runner(
        monkeypatch,
        {
            "JOB_EXECUTION_META": meta,
            "SUPABASE_URL_TEST": "https://example.test.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY_TEST": "test-key",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "prod-key",
        },
    )

    captured = _capture_client(monkeypatch, module)
    module._build_supabase_client()

    assert captured["url"] == "https://example.test.supabase.co"
    assert captured["key"] == "test-key"
