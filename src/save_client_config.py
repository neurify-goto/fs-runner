#!/usr/bin/env python3
"""client_config 正規化 CLI.

GitHub Actions / Cloud Run dispatcher などから渡されるイベント JSON から
client_config を抽出し、構造検証のうえ安全な JSON ファイルとして保存する。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from form_sender.config_validation import (
    ClientConfigValidationError,
    transform_client_config,
)

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate and persist GAS client_config payload")
    parser.add_argument(
        "--input-json",
        default=None,
        help="Path to event JSON, '-' for stdin, or raw JSON string (defaults to $GITHUB_EVENT_PATH)",
    )
    parser.add_argument(
        "--output-path",
        default=None,
        help="Destination file path for the validated client_config (defaults to temp file)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Store JSON with indent=2 (defaults to compact form)",
    )
    return parser.parse_args()


def load_event_data(source: str) -> Dict[str, Any]:
    if source == "-":
        try:
            return json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            raise ValueError(f"STDIN JSON decode error: {exc}") from exc

    path = Path(source)
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON decode error in {source}: {exc}") from exc

    try:
        return json.loads(source)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "--input-json must be a path to an existing file, '-' (stdin), or a JSON string"
        ) from exc


def _parse_client_config_value(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"client_config string is not valid JSON: {exc}") from exc
    raise ValueError("client_config must be provided as dict or JSON string")


def extract_client_config(event_data: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    client_config_raw: Dict[str, Any] | None = None
    targeting_id: Any = None

    inputs = event_data.get("inputs") or {}
    if inputs.get("client_config"):
        client_config_raw = _parse_client_config_value(inputs["client_config"])
        targeting_id = inputs.get("targeting_id")
        logger.info("client_config obtained via workflow_dispatch inputs")

    if client_config_raw is None and event_data.get("client_config"):
        client_config_raw = _parse_client_config_value(event_data["client_config"])
        targeting_id = targeting_id or event_data.get("targeting_id")
        logger.info("client_config obtained from top-level event payload")

    if client_config_raw is None and event_data.get("client_payload"):
        payload = event_data["client_payload"] or {}
        if payload.get("client_config"):
            client_config_raw = _parse_client_config_value(payload["client_config"])
            targeting_id = targeting_id or payload.get("targeting_id")
            logger.info("client_config obtained via repository_dispatch client_payload")

    if client_config_raw is None:
        raise ValueError(
            "client_config not found in event payload. Expected at inputs.client_config, "
            "client_payload.client_config, or top-level client_config",
        )

    if targeting_id is None:
        raise ValueError("targeting_id not found in event payload")

    try:
        targeting_id_int = int(targeting_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"targeting_id is not an integer: {targeting_id}") from exc

    return client_config_raw, targeting_id_int


def atomic_write_json(output_path: Path, data: Dict[str, Any], pretty: bool = False) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=f"{output_path.name}.", suffix=f".{uuid.uuid4().hex[:8]}", dir=str(output_path.parent)
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)

    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2 if pretty else None)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, output_path)
        os.chmod(output_path, 0o600)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def write_targeting_env(targeting_id: int) -> None:
    github_env = os.environ.get("GITHUB_ENV")
    if not github_env:
        return
    try:
        with open(github_env, "a", encoding="utf-8") as handle:
            handle.write(f"TARGETING_ID={targeting_id}\n")
    except OSError as exc:
        logger.warning("Failed to append TARGETING_ID to $GITHUB_ENV: %s", exc)


def _resolve_input_source(arg_value: Optional[str]) -> str:
    if arg_value:
        return arg_value
    env_path = os.getenv("GITHUB_EVENT_PATH")
    if env_path:
        return env_path
    raise ValueError("GITHUB_EVENT_PATH is not set and --input-json was not provided")


def _resolve_output_path(arg_value: Optional[str]) -> Path:
    if arg_value:
        return Path(arg_value)

    env_value = os.getenv("FORM_SENDER_CLIENT_CONFIG_PATH")
    if env_value:
        return Path(env_value)

    base_dir = Path("/tmp")
    unique_name = f"client_config_{os.getpid()}_{int(time.time()*1_000_000)}_{uuid.uuid4().hex[:8]}.json"
    return base_dir / unique_name


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    args = parse_args()

    try:
        event_source = _resolve_input_source(args.input_json)
        output_path = _resolve_output_path(args.output_path)

        event_data = load_event_data(event_source)
        client_config_raw, targeting_id = extract_client_config(event_data)
        logger.info("client_config extraction complete (targeting_id=%s)", targeting_id)

        validated_config = transform_client_config(client_config_raw)
        logger.info("client_config validation complete (structure verified)")

        atomic_write_json(output_path, validated_config, pretty=args.pretty)
        write_targeting_env(targeting_id)

        file_size = output_path.stat().st_size
        print("✅ クライアント設定ファイルが正常に作成されました")
        print(f"   設定ファイル: {output_path}")
        print(f"   targeting_id: {targeting_id}")
        print(f"   ファイルサイズ: {file_size} bytes")
        print("   データ構造: Gas側2シート構造")
    except (ValueError, ClientConfigValidationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"ERROR: JSON decode failure: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        logger.exception("Unexpected error while saving client_config")
        print(f"ERROR: Unexpected failure: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
