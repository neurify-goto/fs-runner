from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from form_sender.config_validation import ClientConfigValidationError, transform_client_config

from .schemas import FormSenderTask, SignedUrlRefreshRequest
from .service import DispatcherService

logger = logging.getLogger(__name__)

app = FastAPI(title="Form Sender Dispatcher", version="1.0.0")
service = DispatcherService()


@app.get("/healthz")
async def healthcheck() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/form-sender/validate-config")
async def validate_config(payload: Dict[str, Any]) -> Dict[str, str]:
    config = payload.get("client_config")
    if config is None:
        raise HTTPException(status_code=400, detail="client_config field is required")
    try:
        transform_client_config(config)
    except ClientConfigValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok"}


@app.post("/v1/form-sender/tasks")
async def enqueue_form_sender(task: FormSenderTask) -> Dict[str, Any]:
    try:
        result = await run_in_threadpool(service.handle_form_sender_task, task)
        logger.info("enqueued form sender task", extra={"targeting_id": task.targeting_id})
        return result
    except HTTPException:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Dispatcher failed to enqueue task")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/v1/form-sender/signed-url/refresh")
async def refresh_signed_url(payload: SignedUrlRefreshRequest) -> Dict[str, Any]:
    try:
        return await run_in_threadpool(service.refresh_signed_url, payload)
    except HTTPException:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Dispatcher failed to refresh signed URL")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/v1/form-sender/executions")
async def list_form_sender_executions(
    status: str = Query("running"),
    targeting_id: int | None = None,
) -> Dict[str, Any]:
    try:
        return await run_in_threadpool(service.list_executions, status, targeting_id)
    except HTTPException:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Dispatcher failed to list executions")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/v1/form-sender/executions/{execution_id}/cancel")
async def cancel_form_sender_execution(execution_id: str) -> Dict[str, Any]:
    try:
        return await run_in_threadpool(service.cancel_execution, execution_id)
    except HTTPException:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Dispatcher failed to cancel execution", extra={"execution_id": execution_id})
        raise HTTPException(status_code=500, detail=str(exc)) from exc
