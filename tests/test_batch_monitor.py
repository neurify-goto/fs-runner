"""Tests for BatchJobMonitor class."""

import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional
from unittest.mock import MagicMock, Mock, patch

import pytest
from google.cloud import batch_v1

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from src.dispatcher.batch_monitor import (
    BatchJobMonitor,
    JST,
    MAX_SUPABASE_RETRIES,
    MIN_MONITOR_INTERVAL_SECONDS,
    SUPABASE_RETRY_DELAY_SECONDS,
)
from src.dispatcher.config import DispatcherSettings


class MockSupabaseClient:
    """Mock Supabase client for testing."""

    def __init__(self):
        self.executions: Dict[str, Dict] = {}
        self.metadata_calls = []
        self.status_calls = []
        self.get_execution_calls = []
        self.fail_count = 0
        self.fail_until = 0

    def get_execution(self, job_execution_id: str) -> Optional[Dict]:
        """Get execution from mock storage."""
        self.get_execution_calls.append(job_execution_id)
        if self.fail_count < self.fail_until:
            self.fail_count += 1
            raise RuntimeError("Simulated Supabase error")
        return self.executions.get(job_execution_id)

    def update_metadata(self, job_execution_id: str, metadata: Dict) -> None:
        """Update metadata in mock storage."""
        self.metadata_calls.append((job_execution_id, metadata))
        if self.fail_count < self.fail_until:
            self.fail_count += 1
            raise RuntimeError("Simulated Supabase error")

    def update_status(self, job_execution_id: str, status: str, ended_at: str = None) -> None:
        """Update status in mock storage."""
        self.status_calls.append((job_execution_id, status, ended_at))
        if self.fail_count < self.fail_until:
            self.fail_count += 1
            raise RuntimeError("Simulated Supabase error")
        if job_execution_id in self.executions:
            self.executions[job_execution_id]["status"] = status


@pytest.fixture
def mock_settings():
    """Create mock settings."""
    settings = Mock(spec=DispatcherSettings)
    settings.batch_monitor_interval_seconds = 60
    settings.batch_monitor_timeout_seconds = 1800
    return settings


@pytest.fixture
def mock_batch_client():
    """Create mock Batch client."""
    return MagicMock(spec=batch_v1.BatchServiceClient)


@pytest.fixture
def mock_supabase():
    """Create mock Supabase client."""
    return MockSupabaseClient()


@pytest.fixture
def monitor(mock_batch_client, mock_supabase, mock_settings):
    """Create BatchJobMonitor instance."""
    return BatchJobMonitor(
        batch_client=mock_batch_client,
        supabase=mock_supabase,
        settings=mock_settings,
    )


def test_monitor_initialization(monitor, mock_settings):
    """Test monitor initialization."""
    assert monitor._interval == 60
    assert monitor._timeout == 1800
    assert len(monitor._threads) == 0


def test_monitor_interval_minimum_enforcement():
    """Test that monitor enforces minimum interval."""
    settings = Mock(spec=DispatcherSettings)
    settings.batch_monitor_interval_seconds = 10  # Below minimum
    settings.batch_monitor_timeout_seconds = 1800

    monitor = BatchJobMonitor(
        batch_client=MagicMock(),
        supabase=MockSupabaseClient(),
        settings=settings,
    )

    assert monitor._interval == MIN_MONITOR_INTERVAL_SECONDS


def test_schedule_creates_thread(monitor):
    """Test that schedule creates a monitoring thread."""
    job_execution_id = "test-exec-123"
    job_name = "projects/test/locations/us-central1/jobs/test-job"

    monitor.schedule(job_execution_id, job_name)

    # Wait a bit for thread to start
    time.sleep(0.1)

    assert job_execution_id in monitor._threads
    assert monitor._threads[job_execution_id].is_alive()


def test_schedule_avoids_duplicate_threads(monitor, mock_supabase, mock_batch_client):
    """Test that schedule doesn't create duplicate threads."""
    job_execution_id = "test-exec-123"
    job_name = "projects/test/locations/us-central1/jobs/test-job"

    # Add a running execution that stays running
    mock_supabase.executions[job_execution_id] = {"status": "running"}

    # Setup job that stays in RUNNING state
    mock_job = Mock()
    mock_job.status = Mock()
    mock_job.status.state = int(batch_v1.JobStatus.State.RUNNING)
    mock_batch_client.get_job.return_value = mock_job

    # Schedule first thread
    monitor.schedule(job_execution_id, job_name)

    # Give it a moment to start and register
    time.sleep(0.1)

    first_thread = monitor._threads.get(job_execution_id)
    assert first_thread is not None
    assert first_thread.is_alive()

    # Schedule second thread - should not create duplicate
    monitor.schedule(job_execution_id, job_name)
    second_thread = monitor._threads.get(job_execution_id)

    # Should be the same thread (second schedule should not create duplicate)
    assert first_thread is second_thread

    # Clean up by marking execution as completed
    mock_supabase.executions[job_execution_id] = {"status": "completed"}
    time.sleep(0.2)  # Let thread exit


def test_monitor_job_success(monitor, mock_batch_client, mock_supabase):
    """Test monitoring a successful job."""
    job_execution_id = "test-exec-success"
    job_name = "projects/test/locations/us-central1/jobs/test-job"

    # Setup execution
    mock_supabase.executions[job_execution_id] = {"status": "running"}

    # Setup job status
    mock_job = Mock()
    mock_job.status = Mock()
    mock_job.status.state = int(batch_v1.JobStatus.State.SUCCEEDED)
    mock_job.name = job_name
    mock_batch_client.get_job.return_value = mock_job

    # Schedule monitoring with very short interval for testing
    monitor._interval = 0.1
    monitor._timeout = 5
    monitor.schedule(job_execution_id, job_name)

    # Wait for thread to complete
    time.sleep(0.5)

    # Verify success was recorded
    assert len(mock_supabase.metadata_calls) > 0
    assert len(mock_supabase.status_calls) > 0

    # Check that metadata was updated with SUCCEEDED
    metadata_call = mock_supabase.metadata_calls[-1]
    assert metadata_call[0] == job_execution_id
    assert metadata_call[1]["batch"]["monitor"]["state"] == "SUCCEEDED"

    # Check that status was updated to succeeded
    status_call = mock_supabase.status_calls[-1]
    assert status_call[0] == job_execution_id
    assert status_call[1] == "succeeded"


def test_monitor_job_failure(monitor, mock_batch_client, mock_supabase):
    """Test monitoring a failed job."""
    job_execution_id = "test-exec-fail"
    job_name = "projects/test/locations/us-central1/jobs/test-job"

    # Setup execution
    mock_supabase.executions[job_execution_id] = {"status": "running"}

    # Setup job status
    mock_job = Mock()
    mock_job.status = Mock()
    mock_job.status.state = int(batch_v1.JobStatus.State.FAILED)
    mock_job.status.status_events = []
    mock_job.name = job_name
    mock_batch_client.get_job.return_value = mock_job

    # Schedule monitoring with very short interval for testing
    monitor._interval = 0.1
    monitor._timeout = 5
    monitor.schedule(job_execution_id, job_name)

    # Wait for thread to complete
    time.sleep(0.5)

    # Verify failure was recorded
    assert len(mock_supabase.metadata_calls) > 0
    assert len(mock_supabase.status_calls) > 0

    # Check that metadata was updated with FAILED
    metadata_call = mock_supabase.metadata_calls[-1]
    assert metadata_call[0] == job_execution_id
    assert metadata_call[1]["batch"]["monitor"]["state"] == "FAILED"
    assert metadata_call[1]["batch"]["monitor"]["reason"] == "batch_failed"

    # Check that status was updated to failed
    status_call = mock_supabase.status_calls[-1]
    assert status_call[0] == job_execution_id
    assert status_call[1] == "failed"


def test_monitor_job_cancellation(monitor, mock_batch_client, mock_supabase):
    """Test monitoring a cancelled job."""
    job_execution_id = "test-exec-cancelled"
    job_name = "projects/test/locations/us-central1/jobs/test-job"

    mock_supabase.executions[job_execution_id] = {"status": "running"}

    mock_job = Mock()
    mock_job.status = Mock()
    mock_job.status.state = int(batch_v1.JobStatus.State.CANCELLATION_IN_PROGRESS)
    mock_job.status.status_events = []
    mock_job.name = job_name
    mock_batch_client.get_job.return_value = mock_job

    monitor._interval = 0.1
    monitor._timeout = 5
    monitor.schedule(job_execution_id, job_name)

    time.sleep(0.5)

    metadata_call = mock_supabase.metadata_calls[-1]
    assert metadata_call[0] == job_execution_id
    assert metadata_call[1]["batch"]["monitor"]["state"] == "CANCELLATION_IN_PROGRESS"
    assert metadata_call[1]["batch"]["monitor"]["reason"] == "batch_cancelled"

    status_call = mock_supabase.status_calls[-1]
    assert status_call[0] == job_execution_id
    assert status_call[1] == "cancelled"


def test_monitor_keeps_polling_after_supabase_cancellation(monitor, mock_batch_client, mock_supabase):
    """BatchモニターがSupabaseのステータスcancelled後も終端状態を記録することを検証する。"""
    job_execution_id = "test-exec-user-cancelled"
    job_name = "projects/test/locations/us-central1/jobs/test-job"

    mock_supabase.executions[job_execution_id] = {
        "status": "running",
        "metadata": {
            "batch": {
                "monitor": {
                    "state": "monitoring",
                }
            }
        },
    }

    states = [
        batch_v1.JobStatus.State.RUNNING,
        batch_v1.JobStatus.State.CANCELLATION_IN_PROGRESS,
    ]

    def job_side_effect(*_args, **_kwargs):
        state = states.pop(0) if states else batch_v1.JobStatus.State.CANCELLATION_IN_PROGRESS
        job = Mock()
        job.status = Mock()
        job.status.state = int(state)
        job.status.status_events = []
        job.name = job_name
        return job

    mock_batch_client.get_job.side_effect = job_side_effect

    monitor._interval = 0.05
    monitor._timeout = 1
    monitor.schedule(job_execution_id, job_name)

    # 最初のポーリング後にユーザーキャンセルでSupabaseが先にcancelledへ遷移したケースを再現
    time.sleep(0.1)
    mock_supabase.executions[job_execution_id]["status"] = "cancelled"

    time.sleep(0.4)

    # 終端状態が記録されていること
    metadata_call = mock_supabase.metadata_calls[-1]
    assert metadata_call[0] == job_execution_id
    monitor_state = metadata_call[1]["batch"]["monitor"]["state"]
    assert monitor_state == "CANCELLATION_IN_PROGRESS"

    # Supabaseステータス更新が継続されていること
    status_call = mock_supabase.status_calls[-1]
    assert status_call[0] == job_execution_id
    assert status_call[1] == "cancelled"


def test_monitor_job_timeout(monitor, mock_batch_client, mock_supabase):
    """Test monitoring timeout."""
    job_execution_id = "test-exec-timeout"
    job_name = "projects/test/locations/us-central1/jobs/test-job"

    # Setup execution
    mock_supabase.executions[job_execution_id] = {"status": "running"}

    # Setup job status (always running, never terminates)
    mock_job = Mock()
    mock_job.status = Mock()
    mock_job.status.state = int(batch_v1.JobStatus.State.RUNNING)
    mock_job.name = job_name
    mock_batch_client.get_job.return_value = mock_job

    # Schedule monitoring with very short timeout
    monitor._interval = 0.1
    monitor._timeout = 0.3
    monitor.schedule(job_execution_id, job_name)

    # Wait for timeout
    time.sleep(0.6)

    # Verify timeout was recorded
    assert len(mock_supabase.metadata_calls) > 0
    assert len(mock_supabase.status_calls) > 0

    # Check that metadata includes timeout
    metadata_call = mock_supabase.metadata_calls[-1]
    assert metadata_call[0] == job_execution_id
    assert metadata_call[1]["batch"]["monitor"]["state"] == "TIMEOUT"
    assert metadata_call[1]["batch"]["monitor"]["reason"] == "batch_timeout"


def test_monitor_exits_when_execution_not_running(monitor, mock_supabase):
    """Test monitor exits when execution is no longer running."""
    job_execution_id = "test-exec-completed"
    job_name = "projects/test/locations/us-central1/jobs/test-job"

    # Setup execution as completed
    mock_supabase.executions[job_execution_id] = {"status": "completed"}

    # Schedule monitoring
    monitor._interval = 0.1
    monitor.schedule(job_execution_id, job_name)

    # Wait for thread to complete
    time.sleep(0.3)

    # Thread should exit immediately without updating anything
    assert job_execution_id not in monitor._threads or not monitor._threads[job_execution_id].is_alive()


def test_get_execution_with_retry_success(monitor, mock_supabase):
    """Test successful execution fetch with retry."""
    job_execution_id = "test-exec-retry"
    mock_supabase.executions[job_execution_id] = {"status": "running"}

    result = monitor._get_execution_with_retry(job_execution_id)

    assert result is not None
    assert result["status"] == "running"
    assert len(mock_supabase.get_execution_calls) == 1


def test_get_execution_with_retry_transient_error(monitor, mock_supabase):
    """Test execution fetch with transient error."""
    job_execution_id = "test-exec-retry"
    mock_supabase.executions[job_execution_id] = {"status": "running"}

    # Fail first 2 attempts, succeed on 3rd
    mock_supabase.fail_until = 2

    with patch("time.sleep"):  # Speed up test
        result = monitor._get_execution_with_retry(job_execution_id)

    assert result is not None
    assert result["status"] == "running"
    # Should have retried
    assert len(mock_supabase.get_execution_calls) == 3


def test_get_execution_with_retry_permanent_error(monitor, mock_supabase):
    """Test execution fetch with permanent error."""
    job_execution_id = "test-exec-retry-fail"

    # Fail all attempts
    mock_supabase.fail_until = MAX_SUPABASE_RETRIES

    with patch("time.sleep"):  # Speed up test
        result = monitor._get_execution_with_retry(job_execution_id)

    assert result is None
    assert len(mock_supabase.get_execution_calls) == MAX_SUPABASE_RETRIES


def test_record_success_with_retry(monitor, mock_supabase):
    """Test recording success with retry logic."""
    job_execution_id = "test-exec-success"
    ended_at = datetime.now(timezone.utc).astimezone(JST)

    with patch("time.sleep"):  # Speed up test
        monitor._record_success(job_execution_id, ended_at)

    # Both calls should succeed
    assert len(mock_supabase.metadata_calls) == 1
    assert len(mock_supabase.status_calls) == 1

    # Verify metadata
    assert mock_supabase.metadata_calls[0][1]["batch"]["monitor"]["state"] == "SUCCEEDED"

    # Verify status
    assert mock_supabase.status_calls[0][1] == "succeeded"


def test_cleanup_thread_removes_dead_threads(monitor):
    """Test that cleanup removes dead threads."""
    # Create some mock threads
    dead_thread = Mock(spec=threading.Thread)
    dead_thread.is_alive.return_value = False

    alive_thread = Mock(spec=threading.Thread)
    alive_thread.is_alive.return_value = True

    monitor._threads = {
        "dead-1": dead_thread,
        "dead-2": dead_thread,
        "alive-1": alive_thread,
    }

    monitor._cleanup_thread("dead-1")

    # All dead threads should be removed, alive thread should remain
    assert "dead-1" not in monitor._threads
    assert "dead-2" not in monitor._threads
    assert "alive-1" in monitor._threads


def test_now_jst_iso_returns_jst_timestamp():
    """Test that _now_jst_iso returns JST timestamp."""
    result = BatchJobMonitor._now_jst_iso()

    # Should be ISO format string
    assert isinstance(result, str)

    # Should be parseable as datetime
    dt = datetime.fromisoformat(result)

    # Should be JST timezone
    assert dt.tzinfo == JST


def test_thread_safety(monitor, mock_supabase, mock_batch_client):
    """Test thread-safe operations."""
    job_execution_ids = [f"test-exec-{i}" for i in range(10)]
    job_name = "projects/test/locations/us-central1/jobs/test-job"

    # Setup executions
    for exec_id in job_execution_ids:
        mock_supabase.executions[exec_id] = {"status": "running"}

    # Setup job to succeed immediately
    mock_job = Mock()
    mock_job.status = Mock()
    mock_job.status.state = int(batch_v1.JobStatus.State.SUCCEEDED)
    mock_batch_client.get_job.return_value = mock_job

    # Schedule multiple monitors concurrently
    monitor._interval = 0.05
    monitor._timeout = 2

    for exec_id in job_execution_ids:
        monitor.schedule(exec_id, job_name)

    # Wait for all to complete
    time.sleep(1)

    # All threads should have completed
    active_threads = sum(1 for t in monitor._threads.values() if t.is_alive())
    assert active_threads == 0
