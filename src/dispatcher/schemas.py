from __future__ import annotations

from base64 import b64encode
from datetime import datetime, timezone
import re
from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, HttpUrl, root_validator, validator


class TableConfig(BaseModel):
    use_extra_table: bool = Field(default=False)
    company_table: str = Field(default="companies")
    send_queue_table: str = Field(default="send_queue")
    submissions_table: Optional[str] = None


_BRANCH_PATTERN = re.compile(r'^[A-Za-z0-9/_.-]+$')


class ExecutionConfig(BaseModel):
    run_total: int = Field(ge=1)
    parallelism: int = Field(ge=1)
    run_index_base: int = Field(ge=0)
    shards: int = Field(ge=1)
    workers_per_workflow: int = Field(ge=1)

    @root_validator(skip_on_failure=True)
    def validate_parallelism(cls, values):  # type: ignore[override]
        run_total = values.get("run_total")
        parallelism = values.get("parallelism")
        if run_total is not None and parallelism is not None and parallelism > run_total:
            raise ValueError("parallelism must be less than or equal to run_total")
        return values


class Metadata(BaseModel):
    triggered_at_jst: Optional[str] = None
    gas_trigger: Optional[str] = None


class FormSenderTask(BaseModel):
    execution_id: Optional[str] = Field(default=None)
    targeting_id: int
    client_config_ref: HttpUrl
    client_config_object: str
    tables: TableConfig = Field(default_factory=TableConfig)
    execution: ExecutionConfig
    test_mode: bool = False
    branch: Optional[str] = None
    workflow_trigger: str = Field(default="automated")
    metadata: Metadata = Field(default_factory=Metadata)
    cpu_class: Optional[str] = Field(default=None)

    @validator("client_config_object")
    def validate_gcs_uri(cls, value: str) -> str:  # type: ignore[override]
        if not value.startswith("gs://"):
            raise ValueError("client_config_object must be a gs:// URI")
        return value

    @validator("branch")
    def validate_branch_name(cls, value: Optional[str]) -> Optional[str]:  # type: ignore[override]
        if value is None:
            return None
        if len(value) > 255:
            raise ValueError("branch name too long")
        if value.startswith('-'):
            raise ValueError("branch cannot start with hyphen")
        if not _BRANCH_PATTERN.match(value):
            raise ValueError("branch must contain only alphanumeric, /, _, ., - characters")
        return value

    @validator("execution_id")
    def validate_execution_id(cls, value: Optional[str]) -> Optional[str]:  # type: ignore[override]
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("execution_id cannot be blank")
        if len(trimmed) > 128:
            raise ValueError("execution_id too long")
        if not re.fullmatch(r"[A-Za-z0-9\-]+", trimmed):
            raise ValueError("execution_id must be alphanumeric or hyphenated")
        return trimmed

    @validator("cpu_class")
    def validate_cpu_class(cls, value: Optional[str]) -> Optional[str]:  # type: ignore[override]
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized not in {"standard", "low"}:
            raise ValueError("cpu_class must be 'standard' or 'low'")
        return normalized

    def job_execution_meta(self) -> str:
        import json

        payload = {
            "run_index_base": self.execution.run_index_base,
            "shards": self.execution.shards,
            "workers_per_workflow": self.execution.workers_per_workflow,
            "test_mode": self.test_mode,
        }
        encoded = b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
        return encoded

    def run_index_key(self) -> str:
        triggered = self.metadata.triggered_at_jst or datetime.now(timezone.utc).isoformat()
        return f"{self.targeting_id}:{self.execution.run_index_base}:{triggered}"

    def gcs_blob_components(self) -> tuple[str, str]:
        parsed = urlparse(self.client_config_object)
        bucket = parsed.netloc
        blob_name = parsed.path.lstrip("/")
        return bucket, blob_name
