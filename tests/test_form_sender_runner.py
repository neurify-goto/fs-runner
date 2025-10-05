import os
import types

import pytest

from src import form_sender_runner as runner


class _FakeQuery:
    def __init__(self, supabase, mode=None):
        self._supabase = supabase
        self._mode = mode
        self._payload = None

    def select(self, *_args, **_kwargs):
        self._mode = "select"
        return self

    def update(self, payload):
        self._mode = "update"
        self._payload = payload
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def execute(self):
        if self._mode == "select":
            return types.SimpleNamespace(data=[{"status": self._supabase.status}])
        if self._mode == "update":
            self._supabase.status = self._payload["status"]
            self._supabase.last_update = self._payload
            return types.SimpleNamespace(data=[self._payload])
        raise AssertionError("Unexpected mode")


class _FakeSupabase:
    def __init__(self, status="running"):
        self.status = status
        self.last_update = None

    def table(self, _name):
        return _FakeQuery(self)


@pytest.fixture(autouse=True)
def _reset_globals(monkeypatch):
    original_job_execution_id = runner.JOB_EXECUTION_ID
    original_failure_flag = runner._failure_recorded
    original_cpu_class = os.environ.get("FORM_SENDER_CPU_CLASS")
    runner._get_cpu_profile_settings.cache_clear()
    try:
        yield
    finally:
        runner.JOB_EXECUTION_ID = original_job_execution_id
        runner._failure_recorded = original_failure_flag
        runner._get_cpu_profile_settings.cache_clear()
        if original_cpu_class is None:
            os.environ.pop("FORM_SENDER_CPU_CLASS", None)
        else:
            os.environ["FORM_SENDER_CPU_CLASS"] = original_cpu_class


def test_update_job_execution_status_skips_success_if_failed(monkeypatch):
    supabase = _FakeSupabase(status="failed")
    monkeypatch.setattr(runner, "_build_supabase_client", lambda: supabase)
    runner.JOB_EXECUTION_ID = "exec-123"

    runner._update_job_execution_status("succeeded")

    assert supabase.last_update is None


def test_mark_job_failed_once_only_updates_first_time(monkeypatch):
    supabase = _FakeSupabase(status="running")
    monkeypatch.setattr(runner, "_build_supabase_client", lambda: supabase)
    runner.JOB_EXECUTION_ID = "exec-456"
    runner._failure_recorded = False

    runner._mark_job_failed_once()
    assert supabase.last_update["status"] == "failed"

    supabase.last_update = None
    runner._mark_job_failed_once()
    assert supabase.last_update is None


def test_resolve_worker_count_respects_cpu_profile(monkeypatch):
    monkeypatch.setenv("FORM_SENDER_CPU_CLASS", "low")
    monkeypatch.setenv("FORM_SENDER_MAX_WORKERS", "4")

    def _fake_worker_config():
        return {"cpu_profiles": {"low": {"max_workers": 1}}}

    # Clear cached profile before injecting fake config
    runner._get_cpu_profile_settings.cache_clear()
    monkeypatch.setattr("config.manager.get_worker_config", lambda: _fake_worker_config())

    assert runner._resolve_worker_count(3, company_id=None) == 1
