import os
import sys
import types
from typing import Any, Dict

import pytest


from shared.supabase.metadata import merge_metadata

from typing import Optional

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


class _FakeJobExecutionRepository:
    def __init__(self, metadata=None):
        self.metadata = metadata or {}
        self.patches: list[tuple[str, Dict[str, Any]]] = []

    def patch_metadata(self, execution_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        self.patches.append((execution_id, patch))
        merged = merge_metadata(self.metadata, patch)
        self.metadata = merged
        return {"execution_id": execution_id, "metadata": merged}

    def get_execution(self, execution_id: str) -> Optional[Dict[str, Any]]:
        if self.metadata is None:
            return None
        return {"execution_id": execution_id, "metadata": dict(self.metadata)}


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
    original_repository = runner._JOB_EXECUTION_REPOSITORY
    original_credentials = runner._SUPABASE_CREDENTIALS
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
        runner._JOB_EXECUTION_REPOSITORY = original_repository
        runner._SUPABASE_CREDENTIALS = original_credentials
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
    repository = _FakeJobExecutionRepository(metadata={"foo": "bar"})
    monkeypatch.setattr(runner, "_get_job_execution_repository", lambda: repository)
    runner.JOB_EXECUTION_ID = "exec-789"

    runner._update_job_execution_metadata({"empty_finish": True})

    assert repository.metadata == {"foo": "bar", "empty_finish": True}
    assert repository.patches == [("exec-789", {"empty_finish": True})]


def test_record_preemption_event_increments(monkeypatch):
    repository = _FakeJobExecutionRepository(metadata={"batch": {"preemption_count": 2}})
    monkeypatch.setattr(runner, "_get_job_execution_repository", lambda: repository)
    runner.JOB_EXECUTION_ID = "exec-789"

    runner._record_preemption_event({"preempted": True})

    assert repository.metadata["batch"]["preemption_count"] == 3
    last_patch = repository.patches[-1][1]
    assert last_patch["batch"]["preemption_count"] == 3


def test_resolve_worker_count_respects_cpu_profile(monkeypatch):
    monkeypatch.setenv("FORM_SENDER_CPU_CLASS", "low")
    monkeypatch.setenv("FORM_SENDER_MAX_WORKERS", "4")

    def _fake_worker_config():
        return {"cpu_profiles": {"low": {"max_workers": 1}}}

    # Clear cached profile before injecting fake config
    runner._get_cpu_profile_settings.cache_clear()
    monkeypatch.setattr("config.manager.get_worker_config", lambda: _fake_worker_config())

    assert runner._resolve_worker_count(3, company_id=None) == 1
