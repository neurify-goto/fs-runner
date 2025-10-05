import base64
from datetime import datetime, timedelta, timezone
from typing import Optional
from types import MethodType, SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from fastapi import HTTPException

from dispatcher.config import DispatcherSettings
from dispatcher.schemas import ExecutionConfig, FormSenderTask, Metadata, TableConfig
from dispatcher.service import DispatcherService
from dispatcher.gcp import SignedUrlManager


def _task_payload_dict(
    issue_time: datetime,
    *,
    ref_url: Optional[str] = None,
    object_path: str = "config.json",
    execution_id: Optional[str] = None,
):
    base_ref = ref_url or (
        "https://storage.googleapis.com/fs-bucket/"
        f"{object_path}?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Date={issue_time.strftime('%Y%m%dT%H%M%SZ')}"
        "&X-Goog-Expires=54000"
    )
    payload = {
        "targeting_id": 42,
        "client_config_ref": base_ref,
        "client_config_object": f"gs://fs-bucket/{object_path}",
        "tables": {"use_extra_table": False, "company_table": "companies", "send_queue_table": "send_queue"},
        "execution": {
            "run_total": 3,
            "parallelism": 3,
            "run_index_base": 0,
            "shards": 8,
            "workers_per_workflow": 4,
        },
        "test_mode": False,
        "workflow_trigger": "automated",
        "metadata": {"triggered_at_jst": issue_time.isoformat()},
    }
    if execution_id:
        payload["execution_id"] = execution_id
    return payload


def _task_payload(issue_time: datetime, **kwargs):
    return FormSenderTask.parse_obj(_task_payload_dict(issue_time, **kwargs))


def test_job_execution_meta_round_trip():
    task = _task_payload(datetime.now(timezone.utc))
    decoded = base64.b64decode(task.job_execution_meta()).decode("utf-8")
    assert "run_index_base" in decoded
    assert "shards" in decoded


def test_signed_url_manager_resign_threshold(monkeypatch):
    issue_time = datetime.now(timezone.utc) - timedelta(hours=1)
    task = _task_payload(issue_time)

    class _FakeBlob:
        def __init__(self):
            self.regenerated = False

        def generate_signed_url(self, **kwargs):
            self.regenerated = True
            return "https://example.com/new-url"

    class _FakeBucket:
        def __init__(self, blob):
            self._blob = blob

        def blob(self, name):
            return self._blob

    class _FakeStorage:
        def __init__(self, blob):
            self._blob = blob

        def bucket(self, name):
            return _FakeBucket(self._blob)

    blob = _FakeBlob()
    storage = _FakeStorage(blob)
    settings = DispatcherSettings(
        project_id="proj",
        location="asia-northeast1",
        job_name="form-sender-runner",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="supabase-key",
        signed_url_refresh_threshold_seconds=1800,
        client_config_bucket="fs-bucket",
    )

    class _Response:
        def __init__(self, status_code: int):
            self.status_code = status_code

    monkeypatch.setattr('dispatcher.gcp.requests.head', lambda url, timeout=10: _Response(200))

    manager = SignedUrlManager(storage_client=storage, settings=settings)
    refreshed = manager.ensure_fresh(task)
    assert refreshed.startswith("https://storage.googleapis.com")  # still fresh enough, no regen
    assert blob.regenerated is False

    near_expiry = datetime.now(timezone.utc) - timedelta(seconds=54000 - 600)
    task2 = _task_payload(near_expiry)
    monkeypatch.setattr('dispatcher.gcp.requests.head', lambda url, timeout=10: _Response(200))
    refreshed2 = manager.ensure_fresh(task2)
    assert refreshed2 == "https://example.com/new-url"
    assert blob.regenerated is True


def test_signed_url_manager_resign_on_head_failure(monkeypatch):
    issue_time = datetime.now(timezone.utc)
    task = _task_payload(issue_time)

    class _FakeBlob:
        def __init__(self):
            self.regenerated = False

        def generate_signed_url(self, **kwargs):
            self.regenerated = True
            return "https://example.com/new-url"

    class _FakeBucket:
        def __init__(self, blob):
            self._blob = blob

        def blob(self, name):
            return self._blob

    class _FakeStorage:
        def __init__(self, blob):
            self._blob = blob

        def bucket(self, name):
            return _FakeBucket(self._blob)

    blob = _FakeBlob()
    storage = _FakeStorage(blob)
    settings = DispatcherSettings(
        project_id="proj",
        location="asia-northeast1",
        job_name="form-sender-runner",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="supabase-key",
        signed_url_refresh_threshold_seconds=1800,
        client_config_bucket="fs-bucket",
    )

    class _Response:
        def __init__(self, status_code: int):
            self.status_code = status_code

    monkeypatch.setattr('dispatcher.gcp.requests.head', lambda url, timeout=10: _Response(404))

    manager = SignedUrlManager(storage_client=storage, settings=settings)
    refreshed = manager.ensure_fresh(task)
    assert refreshed == "https://example.com/new-url"
    assert blob.regenerated is True


def test_signed_url_manager_resign_failure_raises_value_error(monkeypatch):
    issue_time = datetime.now(timezone.utc)
    task = _task_payload(issue_time)

    class _FailingBlob:
        def __init__(self):
            self.regenerated = False

        def generate_signed_url(self, **kwargs):
            self.regenerated = True
            raise RuntimeError("sign error")

    class _FailingBucket:
        def __init__(self, blob):
            self._blob = blob

        def blob(self, name):
            return self._blob

    class _FailingStorage:
        def __init__(self, blob):
            self._blob = blob

        def bucket(self, name):
            return _FailingBucket(self._blob)

    blob = _FailingBlob()
    storage = _FailingStorage(blob)
    settings = DispatcherSettings(
        project_id="proj",
        location="asia-northeast1",
        job_name="form-sender-runner",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="supabase-key",
        signed_url_refresh_threshold_seconds=1800,
        client_config_bucket="fs-bucket",
    )

    class _Response:
        def __init__(self, status_code: int):
            self.status_code = status_code

    # Force re-sign path by simulating HEAD failure
    monkeypatch.setattr('dispatcher.gcp.requests.head', lambda url, timeout=10: _Response(404))

    manager = SignedUrlManager(storage_client=storage, settings=settings)

    with pytest.raises(ValueError) as excinfo:
        manager.ensure_fresh(task)

    assert "再署名" in str(excinfo.value)
    assert blob.regenerated is True


def test_signed_url_manager_validates_origin_success(monkeypatch):
    issue_time = datetime.now(timezone.utc)
    task = _task_payload(issue_time, object_path="dir/config.json")

    class _FakeBlob:
        def generate_signed_url(self, **kwargs):  # pragma: no cover - should not be called
            raise AssertionError("should not regenerate when URL is valid and head succeeds")

    class _FakeBucket:
        def __init__(self, blob):
            self._blob = blob

        def blob(self, name):
            return self._blob

    class _FakeStorage:
        def __init__(self, blob):
            self._blob = blob

        def bucket(self, name):
            return _FakeBucket(self._blob)

    storage = _FakeStorage(_FakeBlob())
    settings = DispatcherSettings(
        project_id="proj",
        location="asia-northeast1",
        job_name="form-sender-runner",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="supabase-key",
        client_config_bucket="fs-bucket",
    )

    class _Response:
        def __init__(self, status_code: int):
            self.status_code = status_code

    monkeypatch.setattr('dispatcher.gcp.requests.head', lambda url, timeout=10: _Response(200))

    manager = SignedUrlManager(storage_client=storage, settings=settings)
    refreshed = manager.ensure_fresh(task)
    assert refreshed.startswith("https://storage.googleapis.com/fs-bucket/dir/config.json")


@pytest.mark.parametrize(
    "ref_url, error_message",
    [
        ("http://storage.googleapis.com/fs-bucket/config.json?X-Goog-Algorithm=GOOG4-RSA-SHA256", "https scheme"),
        (
            "https://example.com/fs-bucket/config.json?X-Goog-Algorithm=GOOG4-RSA-SHA256",
            "storage.googleapis.com",
        ),
        (
            "https://storage.googleapis.com/other-bucket/config.json?X-Goog-Algorithm=GOOG4-RSA-SHA256",
            "does not match",
        ),
        (
            "https://storage.googleapis.com/fs-bucket/config.json",
            "V4 signed URL",
        ),
    ],
)
def test_signed_url_manager_rejects_invalid_origin(monkeypatch, ref_url, error_message):
    issue_time = datetime.now(timezone.utc)
    payload = _task_payload_dict(issue_time, ref_url=ref_url)
    task = FormSenderTask.parse_obj(payload)

    class _FakeBlob:
        def generate_signed_url(self, **kwargs):  # pragma: no cover - should not reach
            raise AssertionError("should fail before regenerating")

    class _FakeBucket:
        def __init__(self, blob):
            self._blob = blob

        def blob(self, name):
            return self._blob

    class _FakeStorage:
        def __init__(self, blob):
            self._blob = blob

        def bucket(self, name):
            return _FakeBucket(self._blob)

    storage = _FakeStorage(_FakeBlob())
    settings = DispatcherSettings(
        project_id="proj",
        location="asia-northeast1",
        job_name="form-sender-runner",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="supabase-key",
        client_config_bucket="fs-bucket",
    )

    manager = SignedUrlManager(storage_client=storage, settings=settings)

    with pytest.raises(ValueError) as excinfo:
        manager.ensure_fresh(task)
    assert error_message in str(excinfo.value)


def test_branch_validation_allows_safe_branch():
    payload = _task_payload_dict(datetime.now(timezone.utc))
    payload["branch"] = "feature/serverless"
    task = FormSenderTask.parse_obj(payload)
    assert task.branch == "feature/serverless"


@pytest.mark.parametrize("branch", ["--upload-pack=bad", "branch with space", "", "a" * 256])
def test_branch_validation_rejects_unsafe_branch(branch):
    payload = _task_payload_dict(datetime.now(timezone.utc))
    payload["branch"] = branch
    with pytest.raises(ValueError):
        FormSenderTask.parse_obj(payload)


def test_dispatcher_build_env_includes_max_workers():
    task = _task_payload(datetime.now(timezone.utc))
    settings = DispatcherSettings(
        project_id="proj",
        location="asia-northeast1",
        job_name="form-sender-runner",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="supabase-key",
    )

    service = object.__new__(DispatcherService)
    service._settings = settings
    service._secret_manager = None

    env_vars = DispatcherService._build_env(
        service,
        task,
        job_execution_id="exec-123",
        signed_url="https://storage.googleapis.com/fs-bucket/config.json",
    )

    assert env_vars["FORM_SENDER_MAX_WORKERS"] == str(task.execution.workers_per_workflow)
    assert env_vars["FORM_SENDER_CLIENT_CONFIG_OBJECT"] == task.client_config_object
    assert env_vars["FORM_SENDER_ENV"] == "cloud_run"
    assert env_vars["FORM_SENDER_LOG_SANITIZE"] == "1"
    assert env_vars["FORM_SENDER_CPU_CLASS"] == "standard"


def test_dispatcher_build_env_honours_cpu_class_override():
    payload = _task_payload_dict(datetime.now(timezone.utc))
    payload["cpu_class"] = "low"
    task = FormSenderTask.parse_obj(payload)
    settings = DispatcherSettings(
        project_id="proj",
        location="asia-northeast1",
        job_name="form-sender-runner",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="supabase-key",
        default_cpu_class="standard",
    )

    service = object.__new__(DispatcherService)
    service._settings = settings
    service._secret_manager = None

    env_vars = DispatcherService._build_env(
        service,
        task,
        job_execution_id="exec-456",
        signed_url="https://storage.googleapis.com/fs-bucket/config.json",
    )

    assert env_vars["FORM_SENDER_CPU_CLASS"] == "low"


def test_cancel_execution_falls_back_when_run_metadata_stub(monkeypatch):
    service = object.__new__(DispatcherService)
    settings = DispatcherSettings(
        project_id="proj",
        location="asia-northeast1",
        job_name="form-sender-runner",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="supabase-key",
    )
    service._settings = settings

    fake_operation = SimpleNamespace(metadata=SimpleNamespace(Unpack=lambda target: True))

    cancelled = {}

    class _FakeOperationsClient:
        def get_operation(self, name):
            cancelled["get_operation_called"] = name
            return fake_operation

        def cancel_operation(self, name):
            cancelled["cancel_operation_called"] = name

    service._operations_client = _FakeOperationsClient()
    service._executions_client = SimpleNamespace(cancel_execution=lambda request: (_ for _ in ()).throw(RuntimeError("should not hit executions cancel")))
    service._supabase = SimpleNamespace(
        get_execution=lambda execution_id: {
            "execution_id": execution_id,
            "status": "running",
            "metadata": {
                "cloud_run_operation": "operations/op-123",
            },
        },
        update_status=lambda *args, **kwargs: None,
    )

    # Replace RunJobMetadata with stub lacking DESCRIPTOR
    monkeypatch.setattr("dispatcher.gcp.RunJobMetadata", type("_StubMetadata", (), {"name": ""}))

    runner = CloudRunJobRunner.__new__(CloudRunJobRunner)
    runner._operations_client = service._operations_client
    runner._executions_client = service._executions_client
    runner._job_path = "projects/proj/locations/asia-northeast1/jobs/form-sender-runner"
    service._job_runner = runner

    # Should fall back to cancel_operation and not raise
    service.cancel_execution("exec-id")

    assert "cancel_operation_called" in cancelled


def test_handle_form_sender_task_preserves_execution_id(monkeypatch):
    issue_time = datetime.now(timezone.utc)
    provided_execution_id = "test-exec-1234"
    task = _task_payload(issue_time, execution_id=provided_execution_id)

    settings = DispatcherSettings(
        project_id="proj",
        location="asia-northeast1",
        job_name="form-sender-runner",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="supabase-key",
    )

    inserted_records: list[dict] = []

    def _insert_execution(job_execution_id, payload, cloud_run_operation, cloud_run_execution=None):
        inserted_records.append(
            {
                "job_execution_id": job_execution_id,
                "payload": payload,
                "cloud_run_operation": cloud_run_operation,
                "cloud_run_execution": cloud_run_execution,
            }
        )
        return {"execution_id": job_execution_id, "metadata": {}}

    service = object.__new__(DispatcherService)
    service._settings = settings
    service._supabase = SimpleNamespace(
        find_active_execution=lambda targeting_id, run_index_base: None,
        insert_execution=_insert_execution,
        update_status=lambda *args, **kwargs: None,
        update_metadata=lambda job_execution_id, metadata: {"execution_id": job_execution_id, "metadata": metadata},
    )
    service._signed_url_manager = SimpleNamespace(ensure_fresh=lambda task: "https://example.com/config.json")

    operation = SimpleNamespace(name="operations/op-1", metadata=SimpleNamespace(name="projects/demo/locations/asia/jobs/form/executions/exe"))
    service._job_runner = SimpleNamespace(
        run_job=lambda **kwargs: operation,
        extract_execution_name=lambda op: "projects/demo/locations/asia/executions/exe",
    )
    service._secret_manager = None

    captured_env: dict[str, str] = {}

    def _fake_build_env(self, task_obj, job_execution_id, signed_url):
        captured_env["JOB_EXECUTION_ID"] = job_execution_id
        return {"JOB_EXECUTION_ID": job_execution_id, "FORM_SENDER_CLIENT_CONFIG_URL": signed_url}

    service._build_env = MethodType(_fake_build_env, service)

    response = service.handle_form_sender_task(task)

    assert response["job_execution_id"] == provided_execution_id
    assert inserted_records
    assert inserted_records[0]["job_execution_id"] == provided_execution_id
    assert inserted_records[0]["payload"]["execution_id"] == provided_execution_id
    assert captured_env["JOB_EXECUTION_ID"] == provided_execution_id


def test_dispatcher_list_executions_returns_public_fields():
    service = object.__new__(DispatcherService)
    executions = [
        {
            "execution_id": "exec-1",
            "targeting_id": 42,
            "run_index_base": 0,
            "status": "running",
            "started_at": "2025-10-03T00:00:00Z",
            "ended_at": None,
            "task_count": 3,
            "parallelism": 2,
            "shards": 8,
            "workers_per_workflow": 4,
            "metadata": {"cloud_run_execution": "projects/demo/locations/asia-northeast1/jobs/form-sender-runner/executions/foo"},
        }
    ]
    service._supabase = SimpleNamespace(list_executions=lambda status, targeting_id: executions)
    result = service.list_executions()
    assert result["executions"][0]["execution_id"] == "exec-1"
    assert result["executions"][0]["metadata"]["cloud_run_execution"].endswith("/executions/foo")


def test_dispatcher_cancel_execution_success(monkeypatch):
    updates: list[tuple[str, str, Optional[str]]] = []
    service = object.__new__(DispatcherService)
    service._supabase = SimpleNamespace(
        get_execution=lambda exec_id: {
            "execution_id": exec_id,
            "status": "running",
            "metadata": {"cloud_run_execution": "projects/demo/locations/asia/jobs/form-sender-runner/executions/foo"},
        },
        update_status=lambda exec_id, status, ended_at=None: updates.append((exec_id, status, ended_at)),
        list_executions=lambda status, targeting_id: [],
    )
    called = {}
    service._job_runner = SimpleNamespace(cancel_execution=lambda **kwargs: called.setdefault("args", kwargs))

    response = service.cancel_execution("exec-1")

    assert response["status"] == "cancelled"
    assert called["args"]["execution_name"].endswith("/executions/foo")
    assert updates and updates[0][1] == "cancelled"


def test_dispatcher_cancel_execution_requires_identifier():
    service = object.__new__(DispatcherService)
    service._job_runner = SimpleNamespace(cancel_execution=lambda **kwargs: None)
    service._supabase = SimpleNamespace(
        get_execution=lambda exec_id: {"execution_id": exec_id, "status": "running", "metadata": {}},
        list_executions=lambda status, targeting_id: [],
    )

    with pytest.raises(HTTPException) as exc:
        service.cancel_execution("exec-missing")
    assert exc.value.status_code == 400


def test_dispatcher_cancel_execution_noop_for_non_running():
    service = object.__new__(DispatcherService)
    service._job_runner = SimpleNamespace(cancel_execution=lambda **kwargs: None)
    service._supabase = SimpleNamespace(
        get_execution=lambda exec_id: {"execution_id": exec_id, "status": "succeeded", "metadata": {}},
        list_executions=lambda status, targeting_id: [],
    )

    response = service.cancel_execution("exec-2")
    assert response["status"] == "noop"
