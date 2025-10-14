from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from supabase import Client, create_client

from .metadata import merge_metadata


from postgrest.exceptions import APIError

class JobExecutionRepository:
    """Supabase access helper shared between dispatcher and runner."""

    def __init__(self, url: str, key: str) -> None:
        self._client: Client = create_client(url, key)

    def find_active_execution(self, targeting_id: int, run_index_base: int) -> Optional[Dict[str, Any]]:
        response = (
            self._client
            .table("job_executions")
            .select("*")
            .eq("targeting_id", targeting_id)
            .eq("run_index_base", run_index_base)
            .in_("status", ["running", "queued"])
            .limit(1)
            .execute()
        )
        data = response.data or []
        return data[0] if data else None

    def insert_execution(
        self,
        job_execution_id: str,
        payload: Dict[str, Any],
        cloud_run_operation: Optional[str],
        cloud_run_execution: Optional[str] = None,
        execution_mode: str = "cloud_run",
    ) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {
            "workflow_trigger": payload.get("workflow_trigger"),
            "branch": payload.get("branch"),
            "test_mode": payload.get("test_mode", False),
            "execution_mode": execution_mode,
            "client_config_object": payload.get("client_config_object"),
            "client_config_ref": payload.get("client_config_ref"),
        }

        metadata["task_payload"] = {
            key: payload.get(key)
            for key in (
                "targeting_id",
                "client_config_ref",
                "client_config_object",
                "tables",
                "execution",
                "test_mode",
                "branch",
                "workflow_trigger",
                "metadata",
                "cpu_class",
                "mode",
                "batch",
            )
        }

        if cloud_run_operation or cloud_run_execution:
            metadata["cloud_run"] = {
                "operation": cloud_run_operation,
                "execution": cloud_run_execution,
            }

        if execution_mode == "batch":
            metadata.setdefault("batch", {})

        record = {
            "execution_id": job_execution_id,
            "job_type": "form_sender",
            "targeting_id": payload["targeting_id"],
            "run_index_base": payload["execution"]["run_index_base"],
            "task_count": payload["execution"]["run_total"],
            "parallelism": payload["execution"].get("parallelism"),
            "shards": payload["execution"]["shards"],
            "workers_per_workflow": payload["execution"]["workers_per_workflow"],
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "execution_mode": execution_mode,
            "metadata": metadata,
        }

        try:
            response = self._client.table("job_executions").insert(record).execute()
            return response.data[0]
        except APIError as exc:
            if self._is_unique_violation(exc):
                existing = self.get_execution(job_execution_id)
                if existing:
                    return existing
            raise

    @staticmethod
    def _is_unique_violation(exc: APIError) -> bool:
        error_code = getattr(exc, "code", None)
        if error_code == "23505":
            return True

        payload = getattr(exc, "payload", None)
        if isinstance(payload, dict) and payload.get("code") == "23505":
            return True

        for arg in exc.args:
            if isinstance(arg, dict) and arg.get("code") == "23505":
                return True
            if isinstance(arg, str):
                if "23505" in arg:
                    return True
                if "duplicate key" in arg.lower():
                    return True
                try:
                    parsed = json.loads(arg)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict) and parsed.get("code") == "23505":
                    return True

        details = getattr(exc, "details", None)
        if isinstance(details, str) and "duplicate key" in details.lower():
            return True

        return False

    def update_metadata(self, job_execution_id: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
        current = self.get_execution(job_execution_id)
        existing_meta: Dict[str, Any] = {}
        execution_mode: Optional[str] = None
        if current:
            existing_meta = current.get("metadata") or {}
            execution_mode = current.get("execution_mode") or existing_meta.get("execution_mode")

        merged_meta = merge_metadata(existing_meta, metadata)
        if merged_meta.get("execution_mode"):
            execution_mode = merged_meta.get("execution_mode")
        elif metadata.get("execution_mode"):
            execution_mode = metadata["execution_mode"]

        update_payload: Dict[str, Any] = {"metadata": merged_meta}
        if execution_mode:
            update_payload["execution_mode"] = execution_mode
            merged_meta["execution_mode"] = execution_mode

        query = (
            self._client
            .table("job_executions")
            .update(update_payload)
            .eq("execution_id", job_execution_id)
        )
        try:
            response = query.execute()
        except AttributeError:
            # Older postgrest client versions do not support select() after update; re-fetch row instead.
            query.execute()
            response = (
                self._client
                .table("job_executions")
                .select("*")
                .eq("execution_id", job_execution_id)
                .limit(1)
                .execute()
            )

        data = response.data or []
        return data[0] if data else {"execution_id": job_execution_id, "metadata": metadata}

    def patch_metadata(self, job_execution_id: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Alias for update_metadata to emphasise partial update semantics."""

        return self.update_metadata(job_execution_id, metadata)

    def update_parallelism(self, job_execution_id: str, parallelism: int) -> None:
        self._client.table("job_executions").update({"parallelism": parallelism}).eq("execution_id", job_execution_id).execute()

    def find_latest_signed_url(self, targeting_id: int, client_config_object: str) -> Optional[str]:
        response = (
            self._client
            .table("job_executions")
            .select("metadata", "started_at")
            .eq("targeting_id", targeting_id)
            .order("started_at", desc=True)
            .limit(20)
            .execute()
        )
        for record in response.data or []:
            metadata = record.get("metadata") or {}
            if not isinstance(metadata, dict):
                continue
            if metadata.get("client_config_object") != client_config_object:
                continue
            batch_meta = metadata.get("batch") if isinstance(metadata.get("batch"), dict) else {}
            candidates = [
                batch_meta.get("latest_signed_url"),
                metadata.get("client_config_ref"),
            ]
            for candidate in candidates:
                if isinstance(candidate, str) and candidate:
                    return candidate
        return None

    def update_status(self, job_execution_id: str, status: str, ended_at: Optional[str] = None) -> None:
        update_payload: Dict[str, Any] = {"status": status}
        if ended_at:
            update_payload["ended_at"] = ended_at
        self._client.table("job_executions").update(update_payload).eq("execution_id", job_execution_id).execute()

    def list_executions(self, status: Optional[str] = None, targeting_id: Optional[int] = None) -> List[Dict[str, Any]]:
        query = self._client.table("job_executions").select("*")
        if status:
            query = query.eq("status", status)
        if targeting_id is not None:
            query = query.eq("targeting_id", targeting_id)
        response = query.order("started_at", desc=True).limit(100).execute()
        return response.data or []

    def get_execution(self, execution_id: str) -> Optional[Dict[str, Any]]:
        response = (
            self._client
            .table("job_executions")
            .select("*")
            .eq("execution_id", execution_id)
            .limit(1)
            .execute()
        )
        data = response.data or []
        return data[0] if data else None
