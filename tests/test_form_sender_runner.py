import os
import sys
import types

import pytest


class _AsyncPlaywrightStub:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _install_playwright_stubs() -> None:
    if "playwright" not in sys.modules:
        playwright_module = types.ModuleType("playwright")
        async_api_module = types.ModuleType("playwright.async_api")
        async_api_module.async_playwright = lambda: _AsyncPlaywrightStub()
        playwright_module.async_api = async_api_module
        sys.modules["playwright.async_api"] = async_api_module
        sys.modules["playwright"] = playwright_module
    if "playwright_stealth" not in sys.modules:
        sys.modules["playwright_stealth"] = types.SimpleNamespace(Stealth=lambda _page: None)


_install_playwright_stubs()

from src import form_sender_runner as runner


class _FakeQuery:
    def __init__(self, supabase, table_name, mode=None):
        self._supabase = supabase
        self._mode = mode
        self._payload = None
        self._table = table_name
        self._columns = ()

    def select(self, *_args, **_kwargs):
        self._mode = "select"
        self._columns = _args
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
            if self._table == "job_executions":
                if self._columns and any("metadata" in col for col in self._columns if isinstance(col, str)):
                    return types.SimpleNamespace(data=[{"metadata": self._supabase.metadata}])
                return types.SimpleNamespace(data=[{"status": self._supabase.status}])
            raise AssertionError("Unexpected table for select")
        if self._mode == "update":
            if "status" in self._payload:
                self._supabase.status = self._payload["status"]
            if "metadata" in self._payload:
                self._supabase.metadata = self._payload["metadata"]
            self._supabase.last_update = self._payload
            return types.SimpleNamespace(data=[self._payload])
        raise AssertionError("Unexpected mode")


class _FakeSupabase:
    def __init__(self, status="running"):
        self.status = status
        self.last_update = None
        self.metadata = {}

    def table(self, _name):
        return _FakeQuery(self, _name)


@pytest.fixture(autouse=True)
def _reset_globals(monkeypatch):
    original_job_execution_id = runner.JOB_EXECUTION_ID
    original_failure_flag = runner._failure_recorded
    original_cpu_class = os.environ.get("FORM_SENDER_CPU_CLASS")
    original_batch_meta = runner._CURRENT_BATCH_META
    original_run_index = runner._CURRENT_RUN_INDEX
    original_batch_attempt = runner._CURRENT_BATCH_ATTEMPT
    original_preemption_stop = runner._PREEMPTION_STOP
    original_preemption_thread = runner._PREEMPTION_THREAD
    runner._get_cpu_profile_settings.cache_clear()
    try:
        yield
    finally:
        runner.JOB_EXECUTION_ID = original_job_execution_id
        runner._failure_recorded = original_failure_flag
        runner._get_cpu_profile_settings.cache_clear()
        runner._CURRENT_BATCH_META = original_batch_meta
        runner._CURRENT_RUN_INDEX = original_run_index
        runner._CURRENT_BATCH_ATTEMPT = original_batch_attempt
        runner._PREEMPTION_STOP = original_preemption_stop
        runner._PREEMPTION_THREAD = original_preemption_thread
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


def test_update_job_execution_metadata_merges(monkeypatch):
    supabase = _FakeSupabase(status="running")
    supabase.metadata = {"foo": "bar"}
    monkeypatch.setattr(runner, "_build_supabase_client", lambda: supabase)
    runner.JOB_EXECUTION_ID = "exec-789"

    runner._update_job_execution_metadata({"empty_finish": True})

    assert supabase.metadata == {"foo": "bar", "empty_finish": True}


def test_resolve_worker_count_respects_cpu_profile(monkeypatch):
    monkeypatch.setenv("FORM_SENDER_CPU_CLASS", "low")
    monkeypatch.setenv("FORM_SENDER_MAX_WORKERS", "4")

    def _fake_worker_config():
        return {"cpu_profiles": {"low": {"max_workers": 1}}}

    # Clear cached profile before injecting fake config
    runner._get_cpu_profile_settings.cache_clear()
    monkeypatch.setattr("config.manager.get_worker_config", lambda: _fake_worker_config())

    assert runner._resolve_worker_count(3, company_id=None) == 1
