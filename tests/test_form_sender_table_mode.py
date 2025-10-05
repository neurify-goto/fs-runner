import importlib
import os
import sys

import pytest

MODULE_NAME = "form_sender_runner"


def _reload_runner(monkeypatch, env=None):
    env = env or {}
    for var in [
        "FORM_SENDER_TABLE_MODE",
        "FORM_SENDER_TEST_MODE",
        "FORM_SENDER_TABLE_MODE_RESOLVED",
        "COMPANY_TABLE",
        "SEND_QUEUE_TABLE",
        "SUBMISSIONS_TABLE",
        "USE_EXTRA_TABLE",
        "USE_TEST_TABLE",
    ]:
        monkeypatch.delenv(var, raising=False)

    for key, value in env.items():
        monkeypatch.setenv(key, value)

    if MODULE_NAME in sys.modules:
        del sys.modules[MODULE_NAME]

    return importlib.import_module(MODULE_NAME)


def test_apply_table_mode_default(monkeypatch):
    module = _reload_runner(monkeypatch)
    module.apply_table_mode(None)

    assert module.TABLE_MODE == "default"
    assert module.COMPANY_TABLE == "companies"
    assert module.SEND_QUEUE_TABLE == "send_queue"
    assert module.SUBMISSIONS_TABLE == "submissions"
    assert os.getenv("FORM_SENDER_TABLE_MODE_RESOLVED") == "default"
    assert os.getenv("USE_TEST_TABLE") == "0"


def test_apply_table_mode_test_env(monkeypatch):
    module = _reload_runner(monkeypatch, {"FORM_SENDER_TEST_MODE": "1"})
    module.apply_table_mode(None)

    assert module.TABLE_MODE == "test"
    assert module.SEND_QUEUE_TABLE == "send_queue_test"
    assert module.FN_CLAIM == "claim_next_batch_test"
    assert os.getenv("USE_TEST_TABLE") == "1"


def test_apply_table_mode_test_env_overrides_prod_tables(monkeypatch):
    module = _reload_runner(
        monkeypatch,
        {
            "FORM_SENDER_TEST_MODE": "true",
            "SEND_QUEUE_TABLE": "send_queue",
            "SUBMISSIONS_TABLE": "submissions",
            "COMPANY_TABLE": "companies_extra",
        },
    )
    module.apply_table_mode(None)

    assert module.SEND_QUEUE_TABLE == "send_queue_test"
    assert module.SUBMISSIONS_TABLE == "submissions_test"
    assert module.COMPANY_TABLE == "companies"
    assert os.getenv("SEND_QUEUE_TABLE") == "send_queue_test"
    assert os.getenv("SUBMISSIONS_TABLE") == "submissions_test"


def test_apply_table_mode_cli_extra(monkeypatch):
    module = _reload_runner(monkeypatch)
    module.apply_table_mode("extra")

    assert module.TABLE_MODE == "extra"
    assert module.COMPANY_TABLE == "companies_extra"
    assert module.FN_MARK_DONE == "mark_done_extra"
    assert os.getenv("USE_EXTRA_TABLE") == "1"
