import json
import os
from pathlib import Path

import pytest

from utils import gcp_batch


@pytest.fixture(autouse=True)
def _reset_batch_cache():
    gcp_batch.reset_cache()
    yield
    gcp_batch.reset_cache()


def test_extract_batch_meta_primary(monkeypatch):
    monkeypatch.setenv("BATCH_TASK_INDEX", "3")
    monkeypatch.setenv("BATCH_TASK_ATTEMPT", "2")
    monkeypatch.setenv("BATCH_TASK_COUNT", "8")

    meta = gcp_batch.extract_batch_meta(os.environ)
    assert meta.task_index == 3
    assert meta.attempt == 2
    assert meta.array_size == 8


def test_extract_batch_meta_alias(monkeypatch):
    monkeypatch.delenv("BATCH_TASK_INDEX", raising=False)
    monkeypatch.setenv("CLOUD_RUN_TASK_INDEX", "5")
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", "4")

    meta = gcp_batch.extract_batch_meta(os.environ)
    assert meta.task_index == 5
    assert meta.attempt == 4


def test_calculate_run_and_shard():
    run_index = gcp_batch.calculate_run_index(10, 4)
    assert run_index == 15
    shard_index = gcp_batch.calculate_shard_index(run_index, 8)
    assert shard_index == (15 - 1) % 8


def test_get_preemption_config_defaults():
    config = gcp_batch.get_preemption_config()
    assert "endpoint" in config
    assert "header_name" in config
    assert config["initial_backoff_seconds"] >= 1


def test_config_alias_override(monkeypatch, tmp_path):
    custom_path = tmp_path / "gcp_batch.json"
    custom_config = {
        "env_aliases": {
            "task_index": ["CUSTOM_BATCH_INDEX"],
            "attempt": ["CUSTOM_BATCH_ATTEMPT"],
        }
    }
    custom_path.write_text(json.dumps(custom_config), encoding="utf-8")

    monkeypatch.setattr(gcp_batch, "_CONFIG_PATH", custom_path)
    gcp_batch.reset_cache()

    monkeypatch.setenv("CUSTOM_BATCH_INDEX", "7")
    monkeypatch.setenv("CUSTOM_BATCH_ATTEMPT", "11")

    meta = gcp_batch.extract_batch_meta(os.environ)
    assert meta.task_index == 7
    assert meta.attempt == 11

