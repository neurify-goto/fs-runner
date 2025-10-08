"""Utilities for Google Cloud Batch metadata handling.

環境変数から Cloud Batch に関するメタデータを読み取り、
ランナー/エントリポイントが一貫した run_index / shard / attempt 情報を
算出できるようにする。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _PROJECT_ROOT / "config" / "gcp_batch.json"

_DEFAULT_ALIAS_MAP: Dict[str, List[str]] = {
    "task_index": ["BATCH_TASK_INDEX", "CLOUD_RUN_TASK_INDEX"],
    "attempt": ["BATCH_TASK_ATTEMPT", "CLOUD_RUN_TASK_ATTEMPT"],
    "array_size": ["BATCH_TASK_COUNT", "CLOUD_RUN_TASK_COUNT"],
}

_DEFAULT_PREEMPTION_CONFIG: Dict[str, object] = {
    "endpoint": "http://metadata.google.internal/computeMetadata/v1/instance/preempted",
    "header_name": "Metadata-Flavor",
    "header_value": "Google",
    "initial_backoff_seconds": 1,
    "max_backoff_seconds": 30,
}


@dataclass(frozen=True)
class BatchMeta:
    """Represents Batch task metadata derived from environment variables."""

    task_index: Optional[int]
    attempt: Optional[int]
    array_size: Optional[int]


@lru_cache(maxsize=1)
def _load_config() -> Dict[str, object]:
    try:
        with _CONFIG_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
            if isinstance(data, dict):
                return data
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}
    return {}


@lru_cache(maxsize=None)
def _get_aliases(key: str) -> List[str]:
    config = _load_config()
    env_aliases = config.get("env_aliases")
    if isinstance(env_aliases, dict):
        value = env_aliases.get(key)
        if isinstance(value, list):
            aliases: List[str] = []
            for entry in value:
                if isinstance(entry, str) and entry:
                    aliases.append(entry)
            if aliases:
                return aliases
    return list(_DEFAULT_ALIAS_MAP.get(key, []))


def _resolve_first_int(alias_names: Iterable[str], environ: os._Environ[str] | Dict[str, str]) -> Optional[int]:
    for name in alias_names:
        raw = environ.get(name)
        if raw is None or raw == "":
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def extract_batch_meta(environ: os._Environ[str] | Dict[str, str] | None = None) -> BatchMeta:
    """Return Batch metadata (task index / attempt / array size) from environment."""

    source = environ or os.environ
    task_index = _resolve_first_int(_get_aliases("task_index"), source)
    attempt = _resolve_first_int(_get_aliases("attempt"), source)
    array_size = _resolve_first_int(_get_aliases("array_size"), source)
    return BatchMeta(task_index=task_index, attempt=attempt, array_size=array_size)


def calculate_run_index(run_index_base: Optional[int], task_index: Optional[int]) -> Optional[int]:
    if run_index_base is None or task_index is None:
        return None
    return run_index_base + task_index + 1


def calculate_shard_index(run_index: Optional[int], shards: int) -> Optional[int]:
    if run_index is None:
        return None
    if shards <= 0:
        return None
    # run_index is 1-based; shards are 0-based distribution
    return (run_index - 1) % shards


@lru_cache(maxsize=1)
def get_preemption_config() -> Dict[str, object]:
    config = _load_config()
    preemption = config.get("preemption")
    if isinstance(preemption, dict):
        merged = dict(_DEFAULT_PREEMPTION_CONFIG)
        for key, value in preemption.items():
            merged[key] = value
        return merged
    return dict(_DEFAULT_PREEMPTION_CONFIG)


def reset_cache() -> None:
    """Clear cached configuration (used in tests)."""

    _load_config.cache_clear()
    _get_aliases.cache_clear()
    get_preemption_config.cache_clear()

