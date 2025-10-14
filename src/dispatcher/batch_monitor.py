from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from google.api_core import exceptions as google_exceptions
from google.cloud import batch_v1

from .config import DispatcherSettings
from .supabase_client import JobExecutionRepository


JST = timezone(timedelta(hours=9))
MIN_MONITOR_INTERVAL_SECONDS = 30
MAX_SUPABASE_RETRIES = 3
SUPABASE_RETRY_DELAY_SECONDS = 2
CANCELLATION_STATES = {
    batch_v1.JobStatus.State.CANCELLATION_IN_PROGRESS,
    batch_v1.JobStatus.State.CANCELLED,
}
TERMINAL_MONITOR_STATES = {
    "SUCCEEDED",
    "FAILED",
    "TIMEOUT",
    "CANCELLED",
    "CANCELLATION_IN_PROGRESS",
}


class BatchJobMonitor:
    """Background monitor that reconciles Cloud Batch job states with Supabase."""

    def __init__(
        self,
        *,
        batch_client: batch_v1.BatchServiceClient,
        supabase: JobExecutionRepository,
        settings: DispatcherSettings,
    ) -> None:
        self._client = batch_client
        self._supabase = supabase
        # Rate-limit the polling interval to avoid hammering the Batch API.
        requested_interval = settings.batch_monitor_interval_seconds
        if requested_interval < MIN_MONITOR_INTERVAL_SECONDS:
            logging.getLogger(__name__).info(
                "Batch monitor interval below minimum; raising to minimum",
                extra={
                    "requested_interval": requested_interval,
                    "min_interval": MIN_MONITOR_INTERVAL_SECONDS,
                },
            )
        self._interval = max(MIN_MONITOR_INTERVAL_SECONDS, requested_interval)
        self._timeout = max(self._interval, settings.batch_monitor_timeout_seconds)
        self._logger = logging.getLogger(__name__)
        self._threads: Dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def schedule(self, job_execution_id: str, job_name: str) -> None:
        with self._lock:
            existing = self._threads.get(job_execution_id)
            if existing and existing.is_alive():
                self._logger.debug(
                    "Batch monitor already running",
                    extra={"execution_id": job_execution_id},
                )
                return

            thread = threading.Thread(
                target=self._monitor_job,
                args=(job_execution_id, job_name),
                name=f"batch-monitor-{job_execution_id[:8]}",
                daemon=True,  # Daemon thread to avoid hanging on Cloud Run shutdown
            )
            self._threads[job_execution_id] = thread
            thread.start()

    def _monitor_job(self, job_execution_id: str, job_name: str) -> None:
        deadline = time.time() + self._timeout
        metadata_patch = {
            "batch": {
                "monitor": {
                    "state": "monitoring",
                    "started_at": self._now_jst_iso(),
                }
            }
        }
        try:
            self._supabase.update_metadata(job_execution_id, metadata_patch)
        except Exception as exc:  # pragma: no cover - best effort
            self._logger.warning(
                "Failed to record monitor start",
                extra={"execution_id": job_execution_id, "error": str(exc)},
            )

        self._logger.info(
            "Starting Batch monitor",
            extra={"execution_id": job_execution_id, "job_name": job_name},
        )

        while time.time() < deadline:
            execution = self._get_execution_with_retry(job_execution_id)
            if execution is None:
                # Failed to fetch execution after retries; sleep and continue
                time.sleep(self._interval)
                continue

            current_status = execution.get("status", "")
            metadata = execution.get("metadata") or {}
            monitor_state = (
                ((metadata.get("batch") or {}).get("monitor") or {}).get("state") or ""
            ).upper()

            if current_status not in {"running", "cancelled"}:
                # Execution already finished; nothing to do.
                self._logger.info(
                    "Execution no longer running; monitor exiting",
                    extra={"execution_id": job_execution_id, "status": current_status},
                )
                self._cleanup_thread(job_execution_id)
                return

            if current_status == "cancelled" and monitor_state in TERMINAL_MONITOR_STATES:
                self._logger.info(
                    "Execution already has recorded terminal monitor state; monitor exiting",
                    extra={
                        "execution_id": job_execution_id,
                        "status": current_status,
                        "monitor_state": monitor_state,
                    },
                )
                self._cleanup_thread(job_execution_id)
                return

            try:
                job = self._client.get_job(name=job_name)
            except google_exceptions.NotFound:
                self._logger.info(
                    "Batch job no longer exists; treating as cancelled",
                    extra={"execution_id": job_execution_id, "job_name": job_name},
                )
                self._record_cancellation(
                    job_execution_id,
                    "batch_job_not_found",
                    "CANCELLED",
                    None,
                )
                self._cleanup_thread(job_execution_id)
                return
            except google_exceptions.GoogleAPICallError as exc:  # pragma: no cover - network path
                if not getattr(exc, "retryable", False):
                    status_code = getattr(exc, "code", None)
                    state_name = getattr(status_code, "name", None) if status_code else None
                    if not state_name:
                        state_name = exc.__class__.__name__.upper()
                    self._logger.error(
                        "Batch monitor received permanent error; stopping polling",
                        extra={
                            "execution_id": job_execution_id,
                            "job_name": job_name,
                            "error": str(exc),
                            "status_code": getattr(status_code, "name", None),
                        },
                    )
                    self._record_failure(
                        job_execution_id,
                        "batch_monitor_permanent_error",
                        state_name,
                        [],
                    )
                    self._cleanup_thread(job_execution_id)
                    return
                self._logger.warning(
                    "Batch monitor encountered retryable error fetching job",
                    extra={"execution_id": job_execution_id, "error": str(exc)},
                )
                time.sleep(self._interval)
                continue
            except Exception as exc:  # pragma: no cover - network path
                self._logger.warning(
                    "Batch monitor failed to fetch job",
                    extra={"execution_id": job_execution_id, "error": str(exc)},
                )
                time.sleep(self._interval)
                continue

            state = job.status.state
            state_enum = batch_v1.JobStatus.State(state)
            state_name = state_enum.name

            if state_enum == batch_v1.JobStatus.State.SUCCEEDED:
                ended_at = datetime.now(timezone.utc)
                self._logger.info(
                    "Batch job succeeded",
                    extra={"execution_id": job_execution_id, "state": state_name},
                )
                self._record_success(job_execution_id, ended_at)
                self._cleanup_thread(job_execution_id)
                return

            if state_enum == batch_v1.JobStatus.State.FAILED:
                self._logger.warning(
                    "Batch job failed",
                    extra={"execution_id": job_execution_id, "state": state_name},
                )
                self._record_failure(
                    job_execution_id,
                    "batch_failed",
                    state_name,
                    job.status.status_events,
                )
                self._cleanup_thread(job_execution_id)
                return

            if state_enum == batch_v1.JobStatus.State.DELETION_IN_PROGRESS:
                if monitor_state != "DELETION_IN_PROGRESS":
                    self._logger.info(
                        "Batch job deletion in progress; continuing to monitor",
                        extra={
                            "execution_id": job_execution_id,
                            "state": state_name,
                        },
                    )
                    self._record_monitor_progress(job_execution_id, state_name)
                else:
                    self._logger.debug(
                        "Batch job still deleting; waiting for removal",
                        extra={
                            "execution_id": job_execution_id,
                            "state": state_name,
                        },
                    )
                time.sleep(self._interval)
                continue

            if state_enum in CANCELLATION_STATES:
                self._logger.info(
                    "Batch job cancelled",
                    extra={"execution_id": job_execution_id, "state": state_name},
                )
                self._record_cancellation(
                    job_execution_id,
                    "batch_cancelled",
                    state_name,
                    job.status.status_events,
                )
                self._cleanup_thread(job_execution_id)
                return

            time.sleep(self._interval)

        # Timed out waiting for terminal state
        self._logger.warning(
            "Batch job did not reach terminal state within timeout",
            extra={"execution_id": job_execution_id, "timeout_seconds": self._timeout},
        )
        self._record_failure(job_execution_id, "batch_timeout", "TIMEOUT", [])
        self._cleanup_thread(job_execution_id)

    def _record_success(self, job_execution_id: str, ended_at: datetime) -> None:
        """Record successful job completion with retry logic."""
        ended_at_iso = ended_at.isoformat()
        recorded_at = ended_at.astimezone(JST).isoformat()
        metadata_patch = {
            "batch": {
                "monitor": {
                    "state": "SUCCEEDED",
                    "recorded_at": recorded_at,
                }
            }
        }

        # Update metadata with retry
        for attempt in range(MAX_SUPABASE_RETRIES):
            try:
                self._supabase.update_metadata(job_execution_id, metadata_patch)
                break
            except Exception as exc:  # pragma: no cover - best effort
                if attempt == MAX_SUPABASE_RETRIES - 1:
                    self._logger.warning(
                        "Failed to update metadata after retries",
                        extra={"execution_id": job_execution_id, "error": str(exc)},
                    )
                else:
                    time.sleep(SUPABASE_RETRY_DELAY_SECONDS)

        # Update status with retry
        for attempt in range(MAX_SUPABASE_RETRIES):
            try:
                self._supabase.update_status(
                    job_execution_id,
                    "succeeded",
                    ended_at=ended_at_iso,
                )
                break
            except Exception as exc:  # pragma: no cover - best effort
                if attempt == MAX_SUPABASE_RETRIES - 1:
                    self._logger.warning(
                        "Failed to update status after retries",
                        extra={"execution_id": job_execution_id, "error": str(exc)},
                    )
                else:
                    time.sleep(SUPABASE_RETRY_DELAY_SECONDS)

    def _record_failure(
        self,
        job_execution_id: str,
        reason: str,
        state_name: str,
        events: Optional[Sequence[batch_v1.JobStatus.StatusEvent]] = None,
    ) -> None:
        self._record_terminal_state(
            job_execution_id,
            status="failed",
            reason=reason,
            state_name=state_name,
            events=events,
        )

    def _record_cancellation(
        self,
        job_execution_id: str,
        reason: str,
        state_name: str,
        events: Optional[Sequence[batch_v1.JobStatus.StatusEvent]] = None,
    ) -> None:
        self._record_terminal_state(
            job_execution_id,
            status="cancelled",
            reason=reason,
            state_name=state_name,
            events=events,
        )

    def _record_monitor_progress(self, job_execution_id: str, state_name: str) -> None:
        metadata_patch = {
            "batch": {
                "monitor": {
                    "state": state_name,
                    "recorded_at": self._now_jst_iso(),
                }
            }
        }

        for attempt in range(MAX_SUPABASE_RETRIES):
            try:
                self._supabase.update_metadata(job_execution_id, metadata_patch)
                break
            except Exception as exc:  # pragma: no cover - best effort
                if attempt == MAX_SUPABASE_RETRIES - 1:
                    self._logger.warning(
                        "Failed to update monitor progress state",
                        extra={
                            "execution_id": job_execution_id,
                            "state": state_name,
                            "error": str(exc),
                        },
                    )
                else:
                    time.sleep(SUPABASE_RETRY_DELAY_SECONDS)

    def _record_terminal_state(
        self,
        job_execution_id: str,
        status: str,
        reason: Optional[str],
        state_name: str,
        events: Optional[Sequence[batch_v1.JobStatus.StatusEvent]] = None,
    ) -> None:
        execution = self._get_execution_with_retry(job_execution_id)
        if not execution:
            return
        execution_status = execution.get("status")
        if execution_status not in {"running", status}:
            return

        ended_at = datetime.now(timezone.utc)
        ended_at_iso = ended_at.isoformat()
        metadata_monitor: Dict[str, Any] = {
            "state": state_name,
            "events": self._build_event_payload(events),
            "recorded_at": ended_at.astimezone(JST).isoformat(),
        }
        if reason:
            metadata_monitor["reason"] = reason

        metadata_patch = {
            "batch": {
                "monitor": metadata_monitor,
            }
        }

        # Update metadata with retry
        for attempt in range(MAX_SUPABASE_RETRIES):
            try:
                self._supabase.update_metadata(job_execution_id, metadata_patch)
                break
            except Exception as exc:  # pragma: no cover - best effort
                if attempt == MAX_SUPABASE_RETRIES - 1:
                    self._logger.warning(
                        "Failed to update metadata after retries",
                        extra={"execution_id": job_execution_id, "error": str(exc)},
                    )
                else:
                    time.sleep(SUPABASE_RETRY_DELAY_SECONDS)

        # Update status with retry
        for attempt in range(MAX_SUPABASE_RETRIES):
            try:
                self._supabase.update_status(
                    job_execution_id,
                    status,
                    ended_at=ended_at_iso,
                )
                break
            except Exception as exc:  # pragma: no cover - best effort
                if attempt == MAX_SUPABASE_RETRIES - 1:
                    self._logger.warning(
                        "Failed to update status after retries",
                        extra={"execution_id": job_execution_id, "error": str(exc)},
                    )
                else:
                    time.sleep(SUPABASE_RETRY_DELAY_SECONDS)

    @staticmethod
    def _build_event_payload(
        events: Optional[Sequence[batch_v1.JobStatus.StatusEvent]],
    ) -> List[Dict[str, Optional[str]]]:
        event_payload: List[Dict[str, Optional[str]]] = []
        if not events:
            return event_payload

        for event in events:
            description = getattr(event, "description", None) or getattr(event, "message", None)
            event_time = None
            timestamp = getattr(event, "event_time", None)
            if timestamp:
                try:
                    event_time = timestamp.ToDatetime().astimezone(JST).isoformat()
                except Exception:  # pragma: no cover - best effort
                    event_time = None
            event_payload.append(
                {
                    "description": description,
                    "event_time": event_time,
                }
            )

        return event_payload

    def _get_execution_with_retry(self, job_execution_id: str) -> Optional[Dict[str, Any]]:
        """Fetch execution from Supabase with retry logic for transient errors."""
        for attempt in range(MAX_SUPABASE_RETRIES):
            try:
                return self._supabase.get_execution(job_execution_id)
            except Exception as exc:  # pragma: no cover - network path
                if attempt < MAX_SUPABASE_RETRIES - 1:
                    self._logger.debug(
                        "Failed to fetch execution; retrying",
                        extra={
                            "execution_id": job_execution_id,
                            "attempt": attempt + 1,
                            "error": str(exc),
                        },
                    )
                    time.sleep(SUPABASE_RETRY_DELAY_SECONDS)
                else:
                    self._logger.warning(
                        "Failed to fetch execution after retries",
                        extra={
                            "execution_id": job_execution_id,
                            "attempts": MAX_SUPABASE_RETRIES,
                            "error": str(exc),
                        },
                    )
        return None

    def _cleanup_thread(self, job_execution_id: str) -> None:
        """Remove thread from tracking dictionary and perform periodic cleanup."""
        with self._lock:
            # Remove the specified thread
            self._threads.pop(job_execution_id, None)

            # Opportunistically clean up all dead threads
            dead_threads = [
                exec_id for exec_id, thread in self._threads.items()
                if not thread.is_alive()
            ]
            for exec_id in dead_threads:
                self._threads.pop(exec_id, None)

            if dead_threads:
                self._logger.debug(
                    "Cleaned up dead threads",
                    extra={"count": len(dead_threads)},
                )

    @staticmethod
    def _now_jst_iso() -> str:
        return datetime.now(timezone.utc).astimezone(JST).isoformat()
