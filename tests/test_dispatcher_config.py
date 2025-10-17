import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from dispatcher.config import DispatcherSettings


DUMMY_BATCH_NETWORK = "projects/proj/global/networks/form-sender-batch"
DUMMY_BATCH_SUBNETWORK = "projects/proj/regions/asia-northeast1/subnetworks/form-sender-batch"


def _base_settings(**overrides):
    params = dict(
        project_id="proj",
        location="asia-northeast1",
        job_name="form-sender",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="supabase-key",
        dispatcher_base_url="https://dispatcher.example.com",
        dispatcher_audience="https://dispatcher.example.com",
        batch_network=DUMMY_BATCH_NETWORK,
        batch_subnetwork=DUMMY_BATCH_SUBNETWORK,
    )
    params.update(overrides)
    return DispatcherSettings(**params)


def test_batch_secret_environment_with_short_names():
    settings = _base_settings(
        batch_project_id="batch-proj",
        batch_location="asia-northeast1",
        batch_job_template="template",
        batch_task_group="group",
        batch_service_account_email="svc@batch-proj.iam.gserviceaccount.com",
        batch_container_image="asia-docker.pkg.dev/proj/repo/image:latest",
        batch_supabase_url_secret="form_sender_supabase_url",
        batch_supabase_service_role_secret="form_sender_supabase_service_role",
    )

    env = settings.batch_secret_environment()
    assert env["SUPABASE_URL"] == (
        "projects/batch-proj/secrets/form_sender_supabase_url/versions/latest"
    )
    assert env["SUPABASE_SERVICE_ROLE_KEY"] == (
        "projects/batch-proj/secrets/form_sender_supabase_service_role/versions/latest"
    )


def test_batch_secret_environment_with_fully_qualified_ids():
    fqid = "projects/fs-prod/secrets/form_sender_supabase_url"
    settings = _base_settings(
        batch_project_id="fs-prod",
        batch_location="asia-northeast1",
        batch_job_template="template",
        batch_task_group="group",
        batch_service_account_email="svc@fs-prod.iam.gserviceaccount.com",
        batch_container_image="asia-docker.pkg.dev/proj/repo/image:latest",
        batch_supabase_url_secret=fqid,
    )

    env = settings.batch_secret_environment()
    assert env["SUPABASE_URL"] == fqid + "/versions/latest"


def test_batch_secret_environment_preserves_versioned_ids():
    versioned = "projects/fs-prod/secrets/form_sender_supabase_url/versions/3"
    settings = _base_settings(
        batch_project_id="fs-prod",
        batch_location="asia-northeast1",
        batch_job_template="template",
        batch_task_group="group",
        batch_service_account_email="svc@fs-prod.iam.gserviceaccount.com",
        batch_container_image="asia-docker.pkg.dev/proj/repo/image:latest",
        batch_supabase_url_secret=versioned,
    )

    env = settings.batch_secret_environment()
    assert env["SUPABASE_URL"] == versioned


def test_require_batch_configuration_requires_dispatcher_base_url():
    settings = _base_settings(
        dispatcher_base_url="",
        dispatcher_audience="",
        batch_project_id="batch-proj",
        batch_location="asia-northeast1",
        batch_job_template="template",
        batch_task_group="group",
        batch_service_account_email="svc@batch-proj.iam.gserviceaccount.com",
        batch_container_image="asia-docker.pkg.dev/proj/repo/image:latest",
        batch_supabase_url_secret="form_sender_supabase_url",
    )

    with pytest.raises(RuntimeError) as excinfo:
        settings.require_batch_configuration()

    assert "FORM_SENDER_DISPATCHER_BASE_URL" in str(excinfo.value)


def test_require_batch_configuration_requires_network():
    settings = _base_settings(
        batch_project_id="batch-proj",
        batch_location="asia-northeast1",
        batch_job_template="template",
        batch_task_group="group",
        batch_service_account_email="svc@batch-proj.iam.gserviceaccount.com",
        batch_container_image="asia-docker.pkg.dev/proj/repo/image:latest",
        batch_supabase_url_secret="form_sender_supabase_url",
        batch_network=None,
        batch_subnetwork=None,
    )

    with pytest.raises(RuntimeError) as excinfo:
        settings.require_batch_configuration()

    message = str(excinfo.value)
    assert "FORM_SENDER_BATCH_NETWORK" in message
    assert "FORM_SENDER_BATCH_SUBNETWORK" in message
