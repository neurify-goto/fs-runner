import os

from utils import env


def test_runtime_environment_priority(monkeypatch):
    env.reset_cache()
    monkeypatch.delenv("FORM_SENDER_ENV", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert env.get_runtime_environment() == "local"

    env.reset_cache()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert env.get_runtime_environment() == "github_actions"

    env.reset_cache()
    monkeypatch.setenv("FORM_SENDER_ENV", "cloud_run")
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    assert env.get_runtime_environment() == "cloud_run"


def test_should_sanitize_logs_prefers_explicit_flag(monkeypatch):
    env.reset_cache()
    monkeypatch.setenv("FORM_SENDER_LOG_SANITIZE", "0")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert env.should_sanitize_logs() is False

    env.reset_cache()
    monkeypatch.delenv("FORM_SENDER_LOG_SANITIZE", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert env.should_sanitize_logs() is True

    env.reset_cache()
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("FORM_SENDER_LOG_SANITIZE", "yes")
    assert env.should_sanitize_logs() is True


def test_is_ci_environment(monkeypatch):
    env.reset_cache()
    monkeypatch.delenv("FORM_SENDER_ENV", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert env.is_ci_environment() is False

    env.reset_cache()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert env.is_ci_environment() is True

    env.reset_cache()
    monkeypatch.setenv("FORM_SENDER_ENV", "cloud_run")
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    assert env.is_ci_environment() is True
