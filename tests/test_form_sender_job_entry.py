import importlib
import json
import os
import sys
from pathlib import Path
import base64

import pytest

module_name = "bin.form_sender_job_entry"


def _reload_module(monkeypatch, env=None):
    env = env or {}
    for var in (
        "FORM_SENDER_CLIENT_CONFIG_URL",
        "FORM_SENDER_CLIENT_CONFIG_PATH",
        "FORM_SENDER_CLIENT_CONFIG_OBJECT",
        "FORM_SENDER_TARGETING_ID",
        "FORM_SENDER_TOTAL_SHARDS",
        "FORM_SENDER_MAX_WORKERS",
        "FORM_SENDER_RUN_INDEX",
        "FORM_SENDER_WORKERS_FROM_META",
        "FORM_SENDER_GIT_REF",
        "FORM_SENDER_GIT_TOKEN",
        "JOB_EXECUTION_META",
        "JOB_EXECUTION_ID",
        "FORM_SENDER_CPU_CLASS",
    ):
        monkeypatch.delenv(var, raising=False)

    for key, value in env.items():
        monkeypatch.setenv(key, value)

    project_root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(project_root))
    monkeypatch.syspath_prepend(str(project_root / "src"))

    if module_name in sys.modules:
        del sys.modules[module_name]

    return importlib.import_module(module_name)


def test_updates_status_on_fetch_failure(monkeypatch):
    module = _reload_module(
        monkeypatch,
        {
            "FORM_SENDER_CLIENT_CONFIG_URL": "https://example.invalid/config.json",
            "FORM_SENDER_CLIENT_CONFIG_OBJECT": "gs://bucket/config.json",
            "FORM_SENDER_TARGETING_ID": "42",
            "JOB_EXECUTION_ID": "exec-123",
        },
    )

    called = []
    deleted = []

    def _raise_fetch(_url):
        raise RuntimeError("expired")

    monkeypatch.setattr(module, "fetch_client_config", _raise_fetch)
    monkeypatch.setattr(module, "delete_client_config_object", lambda uri: deleted.append(uri))
    monkeypatch.setattr(module, "_update_job_execution_status", lambda status: called.append(status))

    with pytest.raises(RuntimeError):
        module.main()

    assert called == ["failed"]
    assert deleted == []


def test_updates_status_on_validation_failure(monkeypatch):
    module = _reload_module(
        monkeypatch,
        {
            "FORM_SENDER_CLIENT_CONFIG_URL": "https://example.invalid/config.json",
            "FORM_SENDER_CLIENT_CONFIG_OBJECT": "gs://bucket/config.json",
            "FORM_SENDER_TARGETING_ID": "42",
            "JOB_EXECUTION_ID": "exec-456",
        },
    )

    monkeypatch.setattr(module, "fetch_client_config", lambda url: {"client": {}})

    from form_sender.config_validation import ClientConfigValidationError

    def _raise_validation_error(config):
        raise ClientConfigValidationError("invalid")

    called = []
    deleted = []
    monkeypatch.setattr(module, "transform_client_config", _raise_validation_error)
    monkeypatch.setattr(module, "delete_client_config_object", lambda uri: deleted.append(uri))
    monkeypatch.setattr(module, "_update_job_execution_status", lambda status: called.append(status))

    with pytest.raises(ClientConfigValidationError):
        module.main()

    assert called == ["failed"]
    assert deleted == []


def test_deletes_client_config_on_success(monkeypatch, tmp_path):
    env = {
        "FORM_SENDER_CLIENT_CONFIG_URL": "https://example.invalid/config.json",
        "FORM_SENDER_CLIENT_CONFIG_OBJECT": "gs://bucket/config.json",
        "FORM_SENDER_CLIENT_CONFIG_PATH": str(tmp_path / "client_config_primary.json"),
        "FORM_SENDER_TARGETING_ID": "42",
        "FORM_SENDER_MAX_WORKERS": "2",
        "JOB_EXECUTION_ID": "exec-789",
    }
    module = _reload_module(monkeypatch, env)

    monkeypatch.setattr(module, "fetch_client_config", lambda url: {"client": {}})
    monkeypatch.setattr(module, "transform_client_config", lambda config: config)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: None)

    deleted = []
    monkeypatch.setattr(module, "delete_client_config_object", lambda uri: deleted.append(uri))

    module.main()

    assert deleted == ["gs://bucket/config.json"]


def test_delete_failure_raises_when_job_succeeds(monkeypatch, tmp_path):
    env = {
        "FORM_SENDER_CLIENT_CONFIG_URL": "https://example.invalid/config.json",
        "FORM_SENDER_CLIENT_CONFIG_OBJECT": "gs://bucket/config.json",
        "FORM_SENDER_CLIENT_CONFIG_PATH": str(tmp_path / "client_config_primary.json"),
        "FORM_SENDER_TARGETING_ID": "42",
        "FORM_SENDER_MAX_WORKERS": "2",
    }
    module = _reload_module(monkeypatch, env)

    monkeypatch.setattr(module, "fetch_client_config", lambda url: {"client": {}})
    monkeypatch.setattr(module, "transform_client_config", lambda config: config)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: None)

    called = []

    def _raise_delete(uri):
        called.append(uri)
        raise RuntimeError("delete failed")

    monkeypatch.setattr(module, "delete_client_config_object", _raise_delete)

    with pytest.raises(RuntimeError) as excinfo:
        module.main()

    assert "delete failed" in str(excinfo.value)
    assert called == ["gs://bucket/config.json"]


def test_delete_failure_does_not_mask_primary_exception(monkeypatch):
    env = {
        "FORM_SENDER_CLIENT_CONFIG_URL": "https://example.invalid/config.json",
        "FORM_SENDER_CLIENT_CONFIG_OBJECT": "gs://bucket/config.json",
        "FORM_SENDER_TARGETING_ID": "42",
    }
    module = _reload_module(monkeypatch, env)

    def _raise_fetch(_url):
        raise RuntimeError("expired")

    called = []

    def _raise_delete(uri):
        called.append(uri)
        raise RuntimeError("delete failed")

    monkeypatch.setattr(module, "fetch_client_config", _raise_fetch)
    monkeypatch.setattr(module, "delete_client_config_object", _raise_delete)

    with pytest.raises(RuntimeError) as excinfo:
        module.main()

    assert str(excinfo.value) == "expired"
    assert called == []




def test_prepare_workspace_fetches_branch(monkeypatch, tmp_path):
    module = _reload_module(monkeypatch)
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(module, "DEFAULT_WORKSPACE", workspace)

    calls = []

    def _fake_run(cmd, check, env=None, cwd=None):
        calls.append((tuple(cmd), cwd))

    monkeypatch.setattr(module.subprocess, "run", _fake_run)
    module.prepare_workspace("feature/test", None)

    assert calls[0][0] == (
        "git",
        "clone",
        "--depth=1",
        module.REPO_URL,
        str(workspace),
    )
    assert calls[0][1] == str(workspace.parent)
    assert calls[1][0] == ("git", "fetch", "--depth=1", "origin", "feature/test")
    assert calls[1][1] == str(workspace)
    assert calls[2][0] == ("git", "checkout", "--force", "feature/test")
    assert calls[2][1] == str(workspace)


def test_prepare_workspace_detaches_commit(monkeypatch, tmp_path):
    module = _reload_module(monkeypatch)
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(module, "DEFAULT_WORKSPACE", workspace)

    calls = []

    def _fake_run(cmd, check, env=None, cwd=None):
        calls.append((tuple(cmd), cwd))

    monkeypatch.setattr(module.subprocess, "run", _fake_run)
    commit = "0123456789abcdef0123456789abcdef01234567"
    module.prepare_workspace(commit, None)

    assert calls[1][0] == ("git", "fetch", "--depth=1", "origin", commit)
    assert calls[2][0] == ("git", "checkout", "--force", "--detach", commit)


def test_main_preserves_cloud_run_env(monkeypatch, tmp_path):
    meta = {
        "run_index_base": 0,
        "shards": 8,
        "workers_per_workflow": 4,
        "test_mode": False,
    }
    encoded_meta = base64.b64encode(json.dumps(meta).encode("utf-8")).decode("utf-8")

    env = {
        "FORM_SENDER_ENV": "cloud_run",
        "FORM_SENDER_CLIENT_CONFIG_URL": "https://example.invalid/config.json",
        "FORM_SENDER_CLIENT_CONFIG_OBJECT": "gs://bucket/config.json",
        "FORM_SENDER_CLIENT_CONFIG_PATH": str(tmp_path / "client_config_invalid.json"),
        "FORM_SENDER_TARGETING_ID": "42",
        "FORM_SENDER_TOTAL_SHARDS": "8",
        "FORM_SENDER_MAX_WORKERS": "4",
        "JOB_EXECUTION_META": encoded_meta,
        "JOB_EXECUTION_ID": "exec-321",
        "CLOUD_RUN_TASK_INDEX": "1",
    }

    module = _reload_module(monkeypatch, env)

    monkeypatch.setattr(module, "fetch_client_config", lambda url: {"client": {}, "targeting": {"concurrent_workflow": 1}})
    monkeypatch.setattr(module, "transform_client_config", lambda config: config)
    monkeypatch.setattr(module, "atomic_write_json", lambda path, data: None)
    monkeypatch.setattr(module, "_refresh_client_config_url_if_needed", lambda url, obj: url)
    monkeypatch.setattr(module, "prepare_workspace", lambda git_ref, git_token: tmp_path)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "delete_client_config_object", lambda uri: None)
    monkeypatch.setattr(module, "cleanup_workspace", lambda workspace: None)
    monkeypatch.setattr(module, "_update_job_execution_status", lambda status: None)

    module.main()

    assert os.environ.get("FORM_SENDER_ENV") == "cloud_run"
