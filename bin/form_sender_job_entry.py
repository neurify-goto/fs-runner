#!/usr/bin/env python3
"""Cloud Run Job entrypoint for form sender.

- Downloads client_config via signed URL.
- Decodes JOB_EXECUTION_META to compute run index and shard id.
- Optionally clones a Git ref for branch testing.
- Invokes form_sender_runner with appropriate arguments.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

from form_sender.config_validation import (
    ClientConfigValidationError,
    transform_client_config,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKSPACE = Path("/tmp/workspace")
DEFAULT_CLIENT_CONFIG_PATH = Path("/tmp/client_config_primary.json")
REPO_URL = "https://github.com/neurify-goto/fs-runner.git"

GIT_REF_PATTERN = re.compile(r'^[A-Za-z0-9/_.-]+$')
COMMIT_SHA_PATTERN = re.compile(r'^[0-9a-fA-F]{7,40}$')


def _get_env_int(name: str, default: int = 0) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning("環境変数 %s に整数以外の値が設定されています: %s", name, value)
        return default


def decode_job_meta(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = base64.b64decode(raw)
        return json.loads(decoded.decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        logger.error("JOB_EXECUTION_META の復号に失敗しました: %s", exc)
        raise


def fetch_client_config(url: str) -> Dict[str, Any]:
    logger.info("client_config をダウンロード中: %s", url)
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        logger.error("client_config の JSON 解析に失敗しました: %s", exc)
        raise
    return payload


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def prepare_workspace(git_ref: str, git_token: Optional[str]) -> Path:
    git_ref = validate_git_ref(git_ref)
    if DEFAULT_WORKSPACE.exists():
        shutil.rmtree(DEFAULT_WORKSPACE)
    DEFAULT_WORKSPACE.parent.mkdir(parents=True, exist_ok=True)

    clone_env = os.environ.copy()
    clone_env.setdefault("GIT_TERMINAL_PROMPT", "0")
    askpass_path: Optional[Path] = None
    try:
        if git_token:
            askpass_path = Path("/tmp/git-askpass.sh")
            askpass_path.write_text("#!/bin/sh\nexec printf '%s\\n' \"${FORM_SENDER_GIT_TOKEN}\"\n", encoding="utf-8")
            os.chmod(askpass_path, 0o700)
            clone_env["FORM_SENDER_GIT_TOKEN"] = git_token
            clone_env["GIT_ASKPASS"] = str(askpass_path)

        logger.info("Git リポジトリをクローンします: %s (ref=%s)", REPO_URL, git_ref)
        subprocess.run(
            [
                "git",
                "clone",
                "--depth=1",
                REPO_URL,
                str(DEFAULT_WORKSPACE),
            ],
            check=True,
            env=clone_env,
            cwd=str(DEFAULT_WORKSPACE.parent),
        )

        fetch_command = [
            "git",
            "fetch",
            "--depth=1",
            "origin",
            git_ref,
        ]
        subprocess.run(
            fetch_command,
            check=True,
            cwd=str(DEFAULT_WORKSPACE),
            env=clone_env,
        )

        checkout_command = [
            "git",
            "checkout",
            "--force",
        ]
        if _is_commit_ref(git_ref):
            checkout_command.append("--detach")
        checkout_command.append(git_ref)
        subprocess.run(
            checkout_command,
            check=True,
            cwd=str(DEFAULT_WORKSPACE),
            env=clone_env,
        )

        requirements_path = DEFAULT_WORKSPACE / "requirements.txt"
        if requirements_path.exists():
            logger.info("branch 用の依存関係を /tmp/workspace/.venv にインストールします")
            site_packages_dir = DEFAULT_WORKSPACE / ".venv" / "lib"
            site_packages_dir.mkdir(parents=True, exist_ok=True)
            install_env = os.environ.copy()
            install_env.setdefault('PYTHONPATH', str(site_packages_dir))
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--upgrade",
                    "--requirement",
                    str(requirements_path),
                    "--target",
                    str(site_packages_dir),
                ],
                check=True,
                env=install_env,
            )
            existing_pythonpath = os.environ.get('PYTHONPATH')
            combined_pythonpath = str(site_packages_dir)
            if existing_pythonpath:
                combined_pythonpath = combined_pythonpath + os.pathsep + existing_pythonpath
            os.environ['PYTHONPATH'] = combined_pythonpath
        else:
            logger.warning("requirements.txt が見つかりませんでした: %s", requirements_path)
    finally:
        if askpass_path and askpass_path.exists():
            try:
                askpass_path.unlink()
            except OSError:
                logger.warning("git-askpass スクリプトの削除に失敗しました")
        os.environ.pop("FORM_SENDER_GIT_TOKEN", None)

    return DEFAULT_WORKSPACE


def validate_git_ref(ref: str) -> str:
    """Validate that ref is a safe git branch/tag/commit reference."""
    if not ref:
        raise ValueError("Git ref must not be empty")
    if len(ref) > 255:
        raise ValueError(f"Git ref too long: {len(ref)} characters")
    if ref.startswith('-'):
        raise ValueError(f"Git ref cannot start with '-': {ref}")
    if not GIT_REF_PATTERN.match(ref):
        raise ValueError(f"Invalid git ref format: {ref}")
    return ref



def _is_commit_ref(ref: str) -> bool:
    return bool(COMMIT_SHA_PATTERN.fullmatch(ref))


def cleanup_workspace(workspace: Path) -> None:
    if workspace == PROJECT_ROOT:
        return
    try:
        shutil.rmtree(workspace)
    except OSError as exc:
        logger.warning("ワークスペースの削除に失敗しました: %s", exc)


def delete_client_config_object(gcs_uri: str) -> None:
    """指定された client_config オブジェクトを Cloud Storage から削除する。"""
    if not gcs_uri:
        return

    parsed = urlparse(gcs_uri)
    if parsed.scheme != "gs":
        raise ValueError("client_config_object は gs:// 形式である必要があります")

    bucket = parsed.netloc
    blob_name = parsed.path.lstrip("/")
    if not bucket or not blob_name:
        raise ValueError("client_config_object にバケット名とオブジェクト名が含まれていません")

    try:
        from google.api_core import exceptions as gcloud_exceptions
        from google.cloud import storage
    except ImportError as exc:  # pragma: no cover - 実行環境のセットアップ漏れ防止
        raise RuntimeError("google-cloud-storage パッケージがインストールされていません") from exc

    client = storage.Client()
    blob = client.bucket(bucket).blob(blob_name)
    try:
        blob.delete()
        logger.info("client_config オブジェクトを削除しました: %s", gcs_uri)
    except gcloud_exceptions.NotFound:
        logger.info("client_config オブジェクトは既に存在しませんでした: %s", gcs_uri)
    except gcloud_exceptions.GoogleAPICallError as exc:
        logger.error("client_config オブジェクトの削除に失敗しました: %s", exc)
        raise
    except Exception:
        logger.exception("client_config オブジェクト削除中に予期せぬエラーが発生しました")
        raise


def _update_job_execution_status(status: str) -> None:
    job_execution_id = os.getenv("JOB_EXECUTION_ID")
    if not job_execution_id:
        return

    try:
        from form_sender_runner import _build_supabase_client
    except Exception as exc:  # pragma: no cover - 依存関係不足時の安全弁
        logger.warning("Supabase クライアント初期化に失敗したためステータス更新をスキップします: %s", exc)
        return

    try:
        supabase = _build_supabase_client()
        supabase.table('job_executions').update({
            'status': status,
            'ended_at': datetime.now(timezone.utc).isoformat(),
        }).eq('execution_id', job_execution_id).execute()
        logger.info("job_executions を %s に更新しました (entrypoint)", status)
    except Exception as exc:  # pragma: no cover - Supabase 側エラーはジョブ継続を優先
        logger.warning("job_executions ステータス更新に失敗しました: %s", exc)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    os.environ.setdefault("FORM_SENDER_ENV", "cloud_run")
    os.environ.setdefault("FORM_SENDER_LOG_SANITIZE", "1")

    client_config_url = os.getenv("FORM_SENDER_CLIENT_CONFIG_URL")
    if not client_config_url:
        raise RuntimeError("FORM_SENDER_CLIENT_CONFIG_URL が設定されていません")

    client_config_path = Path(os.getenv("FORM_SENDER_CLIENT_CONFIG_PATH", str(DEFAULT_CLIENT_CONFIG_PATH)))
    client_config_object = os.getenv("FORM_SENDER_CLIENT_CONFIG_OBJECT")
    if not client_config_object:
        raise RuntimeError("FORM_SENDER_CLIENT_CONFIG_OBJECT が設定されていません")

    workspace = PROJECT_ROOT
    job_completed = False

    try:
        raw_config = fetch_client_config(client_config_url)
        validated_config = transform_client_config(raw_config)
        atomic_write_json(client_config_path, validated_config)
        logger.info("client_config を保存しました: %s", client_config_path)

        meta = decode_job_meta(os.getenv("JOB_EXECUTION_META"))
        run_index_base = int(meta.get("run_index_base", 0))
        shards = int(meta.get("shards", os.getenv("FORM_SENDER_TOTAL_SHARDS", "1")))
        workers_per_workflow = int(meta.get("workers_per_workflow", os.getenv("FORM_SENDER_MAX_WORKERS", "4")))

        task_index = _get_env_int("CLOUD_RUN_TASK_INDEX", 0)
        run_index = run_index_base + task_index + 1
        shard_id = (run_index - 1) % max(1, shards)

        os.environ["FORM_SENDER_RUN_INDEX"] = str(run_index)
        os.environ["FORM_SENDER_WORKERS_FROM_META"] = str(workers_per_workflow)
        os.environ.setdefault("FORM_SENDER_TARGETING_ID", os.getenv("FORM_SENDER_TARGETING_ID", ""))

        git_ref = os.getenv("FORM_SENDER_GIT_REF")
        git_token = os.getenv("FORM_SENDER_GIT_TOKEN")

        if git_ref:
            workspace = prepare_workspace(git_ref, git_token)

        runner_path = workspace / "src" / "form_sender_runner.py"
        if not runner_path.exists():
            raise RuntimeError(f"form_sender_runner.py が見つかりません: {runner_path}")

        target_id_env = os.getenv("FORM_SENDER_TARGETING_ID")
        if not target_id_env:
            raise RuntimeError("FORM_SENDER_TARGETING_ID が設定されていません")

        command = [
            sys.executable,
            str(runner_path),
            "--targeting-id",
            target_id_env,
            "--config-file",
            str(client_config_path),
            "--num-workers",
            str(workers_per_workflow),
            "--shard-id",
            str(shard_id),
        ]

        env = os.environ.copy()
        env.setdefault("PYTHONPATH", str(workspace / "src"))

        logger.info(
            "form_sender_runner を起動します (run_index=%s, shard_id=%s, workers=%s)",
            run_index,
            shard_id,
            workers_per_workflow,
        )
        subprocess.run(command, check=True, cwd=str(workspace), env=env)
        job_completed = True
    except Exception:
        _update_job_execution_status('failed')
        raise
    finally:
        delete_error: Optional[BaseException] = None
        if job_completed:
            try:
                delete_client_config_object(client_config_object)
            except Exception as exc:
                logger.exception(
                    "client_config オブジェクトの削除に失敗しました (後続手動削除が必要な場合があります)",
                )
                _update_job_execution_status('failed')
                delete_error = exc
        else:
            logger.info("失敗したため client_config オブジェクトを保持します: %s", client_config_object)
        cleanup_workspace(workspace)

        if delete_error is not None:
            raise delete_error


if __name__ == "__main__":
    try:
        main()
    except (requests.RequestException, ClientConfigValidationError, RuntimeError) as exc:
        logger.error("Job entrypoint failed: %s", exc)
        sys.exit(1)
    except Exception as exc:
        logger.exception("Unhandled exception in job entrypoint")
        sys.exit(1)
