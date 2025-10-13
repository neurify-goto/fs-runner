import base64
import logging
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from types import MethodType, SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from fastapi import HTTPException

# Ensure local src/ modules are importable when pytest is run from repo root
ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from google.api_core import exceptions as gcloud_exceptions

from dispatcher.config import DispatcherSettings
from dispatcher.schemas import ExecutionConfig, FormSenderTask, Metadata, SignedUrlRefreshRequest, TableConfig
from dispatcher.service import DispatcherService
from dispatcher.gcp import CloudBatchJobRunner, CloudRunJobRunner, SignedUrlManager
import dispatcher.gcp as gcp_module


from dispatcher.supabase_client import JobExecutionRepository

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


def test_form_sender_task_accepts_long_signed_url():
    issue_time = datetime.now(timezone.utc)
    long_signature = "A" * 4096
    long_url = (
        "https://storage.googleapis.com/fs-bucket/config.json"
        f"?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Date={issue_time.strftime('%Y%m%dT%H%M%SZ')}"
        f"&X-Goog-Expires=54000&X-Goog-Signature={long_signature}"
    )
    task = _task_payload(issue_time, ref_url=long_url)
    assert task.client_config_ref.endswith(long_signature)


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
    "ref_url, error_message, expect_validation_error",
    [
        ("http://storage.googleapis.com/fs-bucket/config.json?X-Goog-Algorithm=GOOG4-RSA-SHA256", "https scheme", True),
        (
            "https://example.com/fs-bucket/config.json?X-Goog-Algorithm=GOOG4-RSA-SHA256",
            "storage.googleapis.com",
            False,
        ),
        (
            "https://storage.googleapis.com/other-bucket/config.json?X-Goog-Algorithm=GOOG4-RSA-SHA256",
            "does not match",
            False,
        ),
        (
            "https://storage.googleapis.com/fs-bucket/config.json",
            "V4 signed URL",
            False,
        ),
    ],
)
def test_signed_url_manager_rejects_invalid_origin(monkeypatch, ref_url, error_message, expect_validation_error):
    issue_time = datetime.now(timezone.utc)
    payload = _task_payload_dict(issue_time, ref_url=ref_url)
    if expect_validation_error:
        with pytest.raises(ValueError) as excinfo:
            FormSenderTask.parse_obj(payload)
        assert error_message in str(excinfo.value)
        return

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
                "cloud_run": {"operation": "operations/op-123"},
            },
        },
        update_status=lambda *args, **kwargs: None,
        find_latest_signed_url=lambda *args, **kwargs: None,
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

    def _insert_execution(job_execution_id, payload, cloud_run_operation, cloud_run_execution=None, execution_mode="cloud_run"):
        inserted_records.append(
            {
                "job_execution_id": job_execution_id,
                "payload": payload,
                "cloud_run_operation": cloud_run_operation,
                "cloud_run_execution": cloud_run_execution,
                "execution_mode": execution_mode,
            }
        )
        return {"execution_id": job_execution_id, "metadata": {"execution_mode": execution_mode}}

    service = object.__new__(DispatcherService)
    service._settings = settings
    service._supabase = SimpleNamespace(
        find_active_execution=lambda targeting_id, run_index_base: None,
        insert_execution=_insert_execution,
        update_status=lambda *args, **kwargs: None,
        update_metadata=lambda job_execution_id, metadata: {"execution_id": job_execution_id, "metadata": metadata},
        find_latest_signed_url=lambda *args, **kwargs: None,
    )
    service._signed_url_manager = SimpleNamespace(ensure_fresh=lambda task, **kwargs: "https://example.com/config.json")

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
    assert response["cloud_run"]["operation"] == "operations/op-1"
    assert inserted_records
    assert inserted_records[0]["job_execution_id"] == provided_execution_id
    assert inserted_records[0]["payload"]["execution_id"] == provided_execution_id
    assert inserted_records[0]["execution_mode"] == "cloud_run"
    assert captured_env["JOB_EXECUTION_ID"] == provided_execution_id


def test_handle_form_sender_task_uses_latest_signed_url_when_available(monkeypatch):
    issue_time = datetime.now(timezone.utc)
    payload_dict = _task_payload_dict(issue_time, execution_id=None)
    payload_dict["mode"] = "batch"
    payload_dict["batch"] = {"enabled": True, "max_parallelism": 2}
    task = FormSenderTask.parse_obj(payload_dict)

    settings = DispatcherSettings(
        project_id="proj",
        location="asia-northeast1",
        job_name="form-sender-runner",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="supabase-key",
        dispatcher_base_url="https://dispatcher.example.com",
        dispatcher_audience="https://dispatcher.example.com",
        batch_project_id="proj",
        batch_location="asia-northeast1",
        batch_job_template="form-sender",
        batch_task_group="group0",
        batch_service_account_email="svc@example.iam.gserviceaccount.com",
        batch_container_image="asia-docker.pkg.dev/project/form-sender:latest",
    )

    stored_url = "https://example.com/latest-signed"
    captured_override: dict[str, Optional[str]] = {}

    def _insert_execution(job_execution_id, payload, cloud_run_operation, cloud_run_execution=None, execution_mode="cloud_run"):
        return {"execution_id": job_execution_id, "metadata": {"execution_mode": execution_mode}}

    patched_metadata: list[Dict[str, Any]] = []

    def _update_metadata(execution_id, metadata):
        patched_metadata.append(metadata)
        return {"execution_id": execution_id, "metadata": metadata}

    service = object.__new__(DispatcherService)
    service._settings = settings
    service._supabase = SimpleNamespace(
        find_active_execution=lambda targeting_id, run_index_base: None,
        find_latest_signed_url=lambda targeting_id, client_config_object: stored_url,
        insert_execution=_insert_execution,
        update_status=lambda *args, **kwargs: None,
        update_metadata=_update_metadata,
    )

    def _capture_signed_url(task_obj, **kwargs):
        captured_override["override_url"] = kwargs.get("override_url")
        return "https://example.com/refreshed"

    service._signed_url_manager = SimpleNamespace(ensure_fresh=_capture_signed_url)

    def _ensure_runner(self):
        job = SimpleNamespace(name="projects/proj/locations/asia/jobs/form-sender/jobs/job123", task_groups=[SimpleNamespace(name="group0")])
        meta = {
            "machine_type": "n2d-custom-4-10240",
            "cpu_milli": 4000,
            "memory_mb": 10240,
            "memory_buffer_mb": 2048,
            "prefer_spot": True,
            "allow_on_demand": True,
            "parallelism": 2,
            "array_size": task.execution.run_total,
        }
        return SimpleNamespace(run_job=lambda **kwargs: (job, meta))

    service._ensure_batch_runner = MethodType(_ensure_runner, service)
    service._job_runner = None
    service._secret_manager = None
    service._build_env = MethodType(lambda self, task_obj, job_execution_id, signed_url: {"JOB_EXECUTION_ID": job_execution_id}, service)

    response = service.handle_form_sender_task(task)

    assert captured_override.get("override_url") == stored_url
    assert response["batch_job_name"].endswith("job123")
    assert patched_metadata
    assert patched_metadata[0]["batch"]["latest_signed_url"] == "https://example.com/refreshed"


def test_find_latest_signed_url_falls_back_to_client_config_ref():
    fallback_url = "https://example.com/fallback"
    metadata = {
        "client_config_object": "gs://fs-bucket/config.json",
        "client_config_ref": fallback_url,
    }
    data = [{"metadata": metadata}]

    class _FakeSelect:
        def __init__(self, payload):
            self._payload = payload

        def select(self, *args, **kwargs):
            return self

        def eq(self, *args, **kwargs):
            return self

        def order(self, *args, **kwargs):
            return self

        def limit(self, *args, **kwargs):
            return self

        def execute(self):
            return SimpleNamespace(data=self._payload)

    repo = object.__new__(JobExecutionRepository)
    repo._client = SimpleNamespace(table=lambda _: _FakeSelect(data))  # type: ignore[attr-defined]

    result = JobExecutionRepository.find_latest_signed_url(
        repo,
        targeting_id=42,
        client_config_object="gs://fs-bucket/config.json",
    )

    assert result == fallback_url


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
            "metadata": {
                "cloud_run_execution": "projects/demo/locations/asia-northeast1/jobs/form-sender-runner/executions/foo",
                "execution_mode": "cloud_run",
            },
        }
    ]
    service._supabase = SimpleNamespace(
        list_executions=lambda status, targeting_id: executions,
        find_latest_signed_url=lambda *args, **kwargs: None,
    )
    result = service.list_executions()
    assert result["executions"][0]["execution_id"] == "exec-1"
    assert result["executions"][0]["metadata"]["cloud_run_execution"].endswith("/executions/foo")
    assert result["executions"][0]["execution_mode"] == "cloud_run"


def test_dispatcher_cancel_execution_success(monkeypatch):
    updates: list[tuple[str, str, Optional[str]]] = []
    service = object.__new__(DispatcherService)
    service._supabase = SimpleNamespace(
        get_execution=lambda exec_id: {
            "execution_id": exec_id,
            "status": "running",
            "metadata": {
                "cloud_run_execution": "projects/demo/locations/asia/jobs/form-sender-runner/executions/foo",
                "execution_mode": "cloud_run",
            },
        },
        update_status=lambda exec_id, status, ended_at=None: updates.append((exec_id, status, ended_at)),
        list_executions=lambda status, targeting_id: [],
        find_latest_signed_url=lambda *args, **kwargs: None,
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
        find_latest_signed_url=lambda *args, **kwargs: None,
    )

    with pytest.raises(HTTPException) as exc:
        service.cancel_execution("exec-missing")
    assert exc.value.status_code == 400


def test_dispatcher_cancel_execution_batch_missing_identifier():
    service = object.__new__(DispatcherService)
    service._supabase = SimpleNamespace(
        get_execution=lambda exec_id: {"execution_id": exec_id, "status": "running", "metadata": {"execution_mode": "batch"}},
        list_executions=lambda status, targeting_id: [],
        find_latest_signed_url=lambda *args, **kwargs: None,
    )

    with pytest.raises(HTTPException) as exc:
        service.cancel_execution("exec-batch-missing")
    assert exc.value.status_code == 400


def test_dispatcher_cancel_execution_batch_success():
    deleted: list[str] = []
    service = object.__new__(DispatcherService)
    service._supabase = SimpleNamespace(
        get_execution=lambda exec_id: {
            "execution_id": exec_id,
            "status": "running",
            "metadata": {
                "execution_mode": "batch",
                "batch": {
                    "job_name": "projects/demo/locations/asia/jobs/form",
                },
            },
        },
        update_status=lambda *args, **kwargs: None,
        list_executions=lambda status, targeting_id: [],
        find_latest_signed_url=lambda *args, **kwargs: None,
    )

    def _ensure_runner(self):
        return SimpleNamespace(delete_job=lambda name: deleted.append(name))

    service._ensure_batch_runner = MethodType(_ensure_runner, service)

    response = service.cancel_execution("exec-batch")
    assert response["status"] == "cancelled"
    assert deleted == ["projects/demo/locations/asia/jobs/form"]


def test_dispatcher_cancel_execution_noop_for_non_running():
    service = object.__new__(DispatcherService)
    service._job_runner = SimpleNamespace(cancel_execution=lambda **kwargs: None)
    service._supabase = SimpleNamespace(
        get_execution=lambda exec_id: {"execution_id": exec_id, "status": "succeeded", "metadata": {}},
        list_executions=lambda status, targeting_id: [],
        find_latest_signed_url=lambda *args, **kwargs: None,
    )

    response = service.cancel_execution("exec-2")
    assert response["status"] == "noop"


def test_refresh_signed_url_updates_metadata():
    service = object.__new__(DispatcherService)
    refreshed_urls: list[tuple[str, Dict[str, Any]]] = []
    service._signed_url_manager = SimpleNamespace(
        refresh_for_object=lambda uri, ttl_hours=None: "https://example.com/new-url"
    )
    service._supabase = SimpleNamespace(
        update_metadata=lambda execution_id, metadata: refreshed_urls.append((execution_id, metadata)),
        find_latest_signed_url=lambda *args, **kwargs: None,
    )

    request = SignedUrlRefreshRequest(
        client_config_object="gs://bucket/config.json",
        execution_id="exec-123",
        signed_url_ttl_hours=72,
    )

    result = service.refresh_signed_url(request)

    assert result["signed_url"] == "https://example.com/new-url"
    assert refreshed_urls[0][0] == "exec-123"
    assert refreshed_urls[0][1]["batch"]["latest_signed_url"] == "https://example.com/new-url"


def test_batch_runner_enforces_minimum_machine_type(caplog):
    settings = DispatcherSettings(
        project_id="proj",
        location="asia-northeast1",
        job_name="form-sender",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="supabase-key",
        dispatcher_base_url="https://dispatcher.example.com",
        dispatcher_audience="https://dispatcher.example.com",
        batch_project_id="proj",
        batch_location="asia-northeast1",
        batch_job_template="projects/proj/locations/asia-northeast1/jobs/template",
        batch_task_group="form-sender-workers",
        batch_service_account_email="batch-sa@proj.iam.gserviceaccount.com",
        batch_container_image="asia-northeast1-docker.pkg.dev/proj/repo/image:latest",
        batch_supabase_url_secret="projects/proj/secrets/url",
        batch_supabase_service_role_secret="projects/proj/secrets/key",
        batch_machine_type_default="n2d-custom-4-10240",
    )
    runner = CloudBatchJobRunner(settings)

    payload = _task_payload_dict(datetime.now(timezone.utc))
    payload["mode"] = "batch"
    payload["execution"]["run_total"] = 4
    payload["execution"]["parallelism"] = 4
    payload["execution"]["workers_per_workflow"] = 4
    payload["batch"] = {
        "enabled": True,
        "machine_type": "n2d-custom-2-4096",
    }
    task = FormSenderTask.parse_obj(payload)

    caplog.set_level(logging.WARNING, logger="dispatcher.gcp")
    machine_type, cpu_milli, memory_mb, prefer_spot, allow_on_demand, metadata = runner._calculate_resources(task)

    assert machine_type == "n2d-custom-4-10240"
    assert cpu_milli == 4000
    assert memory_mb == 10240
    assert prefer_spot is True
    assert allow_on_demand is True
    assert metadata.get("memory_warning") is True
    assert metadata.get("computed_memory_mb") == 10240
    assert metadata.get("requested_machine_type") == "n2d-custom-2-4096"
    assert metadata.get("resolved_machine_type") == machine_type
    assert metadata.get("memory_buffer_mb") == settings.batch_memory_buffer_mb_default
    assert "insufficient" in caplog.text


def test_handle_form_sender_task_batch(monkeypatch):
    issue_time = datetime.now(timezone.utc)
    payload = _task_payload_dict(issue_time)
    payload["mode"] = "batch"
    payload["batch"] = {
        "enabled": True,
        "max_parallelism": 2,
        "prefer_spot": True,
        "allow_on_demand_fallback": False,
        "machine_type": "n2d-custom-4-10240",
        "max_attempts": 3,
    }
    task = FormSenderTask.parse_obj(payload)

    settings = DispatcherSettings(
        project_id="proj",
        location="asia-northeast1",
        job_name="form-sender-runner",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="supabase-key",
        dispatcher_base_url="https://dispatcher.example.com",
        dispatcher_audience="https://dispatcher.example.com",
        batch_project_id="proj",
        batch_location="asia-northeast1",
        batch_job_template="form-sender",
        batch_task_group="group0",
        batch_service_account_email="svc@example.iam.gserviceaccount.com",
        batch_container_image="asia-docker.pkg.dev/project/form-sender:latest",
    )

    inserted: list[str] = []

    def _insert(job_execution_id, payload, cloud_run_operation, cloud_run_execution=None, execution_mode="cloud_run"):
        inserted.append(execution_mode)
        return {"execution_id": job_execution_id, "metadata": {"execution_mode": execution_mode}}

    patched_metadata: list[Dict[str, Any]] = []

    def _update_metadata(execution_id, metadata):
        patched_metadata.append(metadata)
        return {"execution_id": execution_id, "metadata": metadata}

    service = object.__new__(DispatcherService)
    service._settings = settings
    service._supabase = SimpleNamespace(
        find_active_execution=lambda targeting_id, run_index_base: None,
        insert_execution=_insert,
        update_status=lambda *args, **kwargs: None,
        update_metadata=_update_metadata,
        find_latest_signed_url=lambda *args, **kwargs: None,
    )
    service._signed_url_manager = SimpleNamespace(ensure_fresh=lambda task, **kwargs: "https://example.com/config.json")

    def _ensure_runner(self):
        job = SimpleNamespace(name="projects/proj/locations/asia-northeast1/jobs/form-sender/jobs/job123", task_groups=[SimpleNamespace(name="group0")])
        meta = {
            "machine_type": "n2d-custom-4-10240",
            "cpu_milli": 4000,
            "memory_mb": 10240,
            "memory_buffer_mb": 2048,
            "prefer_spot": True,
            "allow_on_demand": False,
            "parallelism": 2,
            "array_size": task.execution.run_total,
            "attempts": 3,
            "max_retry_count": 2,
        }
        return SimpleNamespace(run_job=lambda **kwargs: (job, meta))

    service._ensure_batch_runner = MethodType(_ensure_runner, service)
    service._job_runner = None
    service._secret_manager = None
    service._build_env = MethodType(lambda self, task_obj, job_execution_id, signed_url: {"JOB_EXECUTION_ID": job_execution_id}, service)

    response = service.handle_form_sender_task(task)

    assert response["status"] == "queued"
    assert "batch" in response
    assert response["batch"]["job_name"].endswith("job123")
    assert response["batch_job_name"].endswith("job123")
    assert response["batch"]["array_size"] == task.execution.run_total
    assert patched_metadata
    assert patched_metadata[0]["batch"]["latest_signed_url"] == "https://example.com/config.json"
    assert response["batch"]["attempts"] == 3
    assert response["batch"]["max_retry_count"] == 2
    assert response["batch"]["memory_buffer_mb"] == 2048
    assert inserted == ["batch"]


def test_batch_runner_raises_when_job_template_missing(monkeypatch):
    class _StubBatchClient:
        def __init__(self, *args, **kwargs):
            self.created: list[Any] = []

        def get_job(self, name):
            raise gcloud_exceptions.NotFound("template missing")

        def create_job(self, *args, **kwargs):  # pragma: no cover - not reached
            job_name = kwargs.get("job_id", "job")
            job_path = f"projects/demo/locations/asia-northeast1/jobs/{job_name}"
            record = SimpleNamespace(name=job_path, task_groups=[])
            self.created.append(record)
            return record

        def delete_job(self, name):  # pragma: no cover - not reached
            return None

    monkeypatch.setattr(gcp_module.batch_v1, "BatchServiceClient", lambda *args, **kwargs: _StubBatchClient())

    settings = DispatcherSettings(
        project_id="proj",
        location="asia-northeast1",
        job_name="form-sender",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="supabase-key",
        dispatcher_base_url="https://dispatcher.example.com",
        dispatcher_audience="https://dispatcher.example.com",
        batch_project_id="proj",
        batch_location="asia-northeast1",
        batch_job_template="projects/proj/locations/asia-northeast1/jobs/form-sender-template",
        batch_task_group="group0",
        batch_service_account_email="svc@example.iam.gserviceaccount.com",
        batch_container_image="asia/artifact/form-sender:latest",
        batch_supabase_url_secret="projects/proj/secrets/url",
        batch_supabase_service_role_secret="projects/proj/secrets/key",
    )
    runner = CloudBatchJobRunner(settings)

    payload = _task_payload_dict(datetime.now(timezone.utc))
    payload["mode"] = "batch"
    payload["batch"] = {"enabled": True}
    task = FormSenderTask.parse_obj(payload)

    with pytest.raises(RuntimeError) as excinfo:
        runner.run_job(
            task=task,
            env_vars={},
            task_count=task.execution.run_total,
            parallelism=task.effective_parallelism(),
        )

    assert "job template" in str(excinfo.value)


def test_calculate_resources_warns_when_memory_below_recommendation(caplog):
    settings = DispatcherSettings(
        project_id="proj",
        location="asia-northeast1",
        job_name="form-sender",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="supabase-key",
        dispatcher_base_url="https://dispatcher.example.com",
        dispatcher_audience="https://dispatcher.example.com",
        batch_project_id="proj",
        batch_location="asia-northeast1",
        batch_job_template="form-sender",
        batch_task_group="group0",
        batch_service_account_email="svc@example.iam.gserviceaccount.com",
        batch_container_image="asia/artifact/form-sender:latest",
        batch_supabase_url_secret="projects/proj/secrets/url",
        batch_supabase_service_role_secret="projects/proj/secrets/key",
        batch_memory_per_worker_mb_default=1024,
    )
    runner = CloudBatchJobRunner(settings)

    payload = _task_payload_dict(datetime.now(timezone.utc))
    payload["mode"] = "batch"
    payload["execution"]["run_total"] = 4
    payload["execution"]["parallelism"] = 4
    payload["execution"]["workers_per_workflow"] = 4
    payload["batch"] = {"enabled": True}
    task = FormSenderTask.parse_obj(payload)

    caplog.set_level(logging.WARNING, logger="dispatcher.gcp")
    machine_type, cpu_milli, memory_mb, prefer_spot, allow_on_demand, metadata = runner._calculate_resources(task)

    assert machine_type.endswith("-6144")
    assert memory_mb < 8192
    assert metadata.get("memory_warning") is True
    assert metadata.get("computed_memory_mb") == memory_mb
    assert metadata.get("memory_buffer_mb") == settings.batch_memory_buffer_mb_default
    assert metadata.get("recommended_memory_mb") == 8192
    assert "below recommended minimum" in caplog.text


def test_calculate_resources_honours_payload_memory_buffer():
    settings = DispatcherSettings(
        project_id="proj",
        location="asia-northeast1",
        job_name="form-sender",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="supabase-key",
        dispatcher_base_url="https://dispatcher.example.com",
        dispatcher_audience="https://dispatcher.example.com",
        batch_project_id="proj",
        batch_location="asia-northeast1",
        batch_job_template="form-sender",
        batch_task_group="group0",
        batch_service_account_email="svc@example.iam.gserviceaccount.com",
        batch_container_image="asia/artifact/form-sender:latest",
        batch_memory_buffer_mb_default=1024,
        batch_supabase_url_secret="projects/proj/secrets/url",
        batch_supabase_service_role_secret="projects/proj/secrets/key",
    )
    runner = CloudBatchJobRunner(settings)

    payload = _task_payload_dict(datetime.now(timezone.utc))
    payload["mode"] = "batch"
    payload["execution"]["run_total"] = 2
    payload["execution"]["parallelism"] = 2
    payload["execution"]["workers_per_workflow"] = 2
    payload["batch"] = {
        "enabled": True,
        "memory_per_worker_mb": 2048,
        "memory_buffer_mb": 4096,
    }
    task = FormSenderTask.parse_obj(payload)

    machine_type, cpu_milli, memory_mb, prefer_spot, allow_on_demand, metadata = runner._calculate_resources(task)

    assert machine_type == "n2d-custom-2-8192"
    assert cpu_milli == 2000
    assert memory_mb == 8192
    assert metadata["memory_buffer_mb"] == 4096


def test_apply_secret_variables_defaults_to_plain_strings():
    from google.cloud import batch_v1

    settings = DispatcherSettings(
        project_id="proj",
        location="asia-northeast1",
        job_name="form-sender",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="supabase-key",
        dispatcher_base_url="https://dispatcher.example.com",
        dispatcher_audience="https://dispatcher.example.com",
        batch_project_id="proj",
        batch_location="asia-northeast1",
        batch_job_template="projects/proj/locations/asia-northeast1/jobs/template",
        batch_task_group="form-sender-workers",
        batch_service_account_email="batch-sa@proj.iam.gserviceaccount.com",
        batch_container_image="asia-northeast1-docker.pkg.dev/proj/repo/image:latest",
        batch_supabase_url_secret="projects/proj/secrets/url",
        batch_supabase_service_role_secret="projects/proj/secrets/key",
    )
    runner = CloudBatchJobRunner(settings)

    environment = batch_v1.Environment()
    secret_path = "projects/proj/secrets/service-role/versions/latest"

    runner._apply_secret_variables(environment, {"SUPABASE_SERVICE_ROLE_KEY": secret_path})

    assert environment.secret_variables["SUPABASE_SERVICE_ROLE_KEY"] == secret_path


def test_batch_mode_normalized_when_batch_payload_present():
    issue_time = datetime.now(timezone.utc)
    payload = _task_payload_dict(issue_time)
    payload.pop("mode", None)
    payload["batch"] = {
        "enabled": False,
        "max_parallelism": 1,
    }

    task = FormSenderTask.parse_obj(payload)

    assert task.mode == "batch"
    assert task.batch is not None
    assert task.batch.enabled is True
