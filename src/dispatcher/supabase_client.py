from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from supabase import Client, create_client


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
            "metadata": {
                "workflow_trigger": payload.get("workflow_trigger"),
                "branch": payload.get("branch"),
                "cloud_run_operation": cloud_run_operation,
                "cloud_run_execution": cloud_run_execution,
                "test_mode": payload.get("test_mode", False),
            },
        }
        response = self._client.table("job_executions").insert(record).execute()
        return response.data[0]

    def update_metadata(self, job_execution_id: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
        response = (
            self._client
            .table("job_executions")
            .update({"metadata": metadata})
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
