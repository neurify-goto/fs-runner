"""環境変数ユーティリティ

Cloud Run / GitHub Actions / ローカル実行間で共通の環境判定処理を提供する。
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

RuntimeEnv = Literal["cloud_run", "github_actions", "gcp_batch", "local"]

FORM_SENDER_ENV_VAR = "FORM_SENDER_ENV"
FORM_SENDER_LOG_SANITIZE_VAR = "FORM_SENDER_LOG_SANITIZE"


@lru_cache(maxsize=None)
def get_runtime_environment() -> RuntimeEnv:
    """現在の実行環境を判定する。

    優先順位:
    1. FORM_SENDER_ENV（cloud_run / github_actions / local 等）
    2. GITHUB_ACTIONS が true の場合は github_actions
    3. 上記以外は local
    """
    explicit_env = os.getenv(FORM_SENDER_ENV_VAR)
    if explicit_env:
        normalized = explicit_env.strip().lower()
        if normalized in {"cloud_run", "github_actions", "gcp_batch", "local"}:
            return normalized  # type: ignore[return-value]
        return "local"

    if os.getenv("GITHUB_ACTIONS", "").lower() == "true":
        return "github_actions"

    return "local"


@lru_cache(maxsize=None)
def should_sanitize_logs() -> bool:
    """ログサニタイズを有効にすべきか判定する。"""
    override = os.getenv(FORM_SENDER_LOG_SANITIZE_VAR)
    if override is not None:
        return override.strip().lower() in {"1", "true", "yes"}

    runtime = get_runtime_environment()

    # GitHub Actions / GCP Batch では常にサニタイズをオンにする
    if runtime in {"github_actions", "gcp_batch"}:
        return True

    return False


def is_cloud_run() -> bool:
    """Cloud Run 上で実行されているか判定。"""
    return get_runtime_environment() == "cloud_run"


def is_github_actions() -> bool:
    """GitHub Actions 上か判定。"""
    return get_runtime_environment() == "github_actions"


def is_gcp_batch() -> bool:
    """GCP Batch 上で実行されているか判定。"""
    return get_runtime_environment() == "gcp_batch"


def is_ci_environment() -> bool:
    """CI（Cloud Run / GitHub Actions）上での実行か判定。"""
    return get_runtime_environment() in {"cloud_run", "github_actions", "gcp_batch"}


def reset_cache() -> None:
    """テスト用にキャッシュをリセットする。"""
    get_runtime_environment.cache_clear()
    should_sanitize_logs.cache_clear()
