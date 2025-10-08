from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from supabase import Client, create_client


def _merge_metadata(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base or {})
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_metadata(merged[key], value)
        else:
            merged[key] = value
    return merged


class JobExecutionRepository:
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
            "metadata": {
                "workflow_trigger": payload.get("workflow_trigger"),
                "branch": payload.get("branch"),
                "cloud_run_operation": cloud_run_operation,
                "cloud_run_execution": cloud_run_execution,
                "test_mode": payload.get("test_mode", False),
                "execution_mode": execution_mode,
            },
        }
        response = self._client.table("job_executions").insert(record).execute()
        return response.data[0]

    def update_metadata(self, job_execution_id: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
        current = self.get_execution(job_execution_id)
        existing_meta: Dict[str, Any] = {}
        execution_mode: Optional[str] = None
        if current:
            existing_meta = current.get("metadata") or {}
            execution_mode = current.get("execution_mode") or existing_meta.get("execution_mode")

        merged_meta = _merge_metadata(existing_meta, metadata)
        if merged_meta.get("execution_mode"):
            execution_mode = merged_meta.get("execution_mode")
        elif metadata.get("execution_mode"):
            execution_mode = metadata["execution_mode"]

        update_payload: Dict[str, Any] = {"metadata": merged_meta}
        if execution_mode:
            update_payload["execution_mode"] = execution_mode
            merged_meta["execution_mode"] = execution_mode
        response = (
            self._client
            .table("job_executions")
            .update(update_payload)
            .eq("execution_id", job_execution_id)
            .select("*")
            .execute()
        )
        data = response.data or []
        return data[0] if data else {"execution_id": job_execution_id, "metadata": metadata}

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
