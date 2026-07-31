import asyncio
import json
import re

from fastapi.testclient import TestClient

from digitalplat_auto_register.core.result import RegistrationResult, StepResult
from digitalplat_auto_register.types import RegistrationStatus
from digitalplat_auto_register import web_app


def run(coroutine):
    return asyncio.run(coroutine)


async def start_and_wait(manager):
    job = await manager.start()
    await job.task
    return job


def completed_result():
    return RegistrationResult(
        success=True,
        username="registered-user",
        email="temporary@example.test",
        registration_status=RegistrationStatus.COMPLETED,
        total_duration=1.25,
    )


def test_success_is_persisted_and_redacted(tmp_path, monkeypatch):
    async def fake_register(referral_code, phone, on_step_complete):
        assert referral_code == web_app.DEFAULT_REFERRAL_CODE
        assert re.fullmatch(r"\+1-\d{10}", phone)
        on_step_complete(
            StepResult(
                name="email_creation",
                status="completed",
                success=True,
                duration=0.5,
                message="Email created: temporary@example.test",
            )
        )
        return completed_result()

    monkeypatch.setattr(web_app, "register_with_defaults", fake_register)
    manager = web_app.RegistrationManager(tmp_path / "jobs.json")
    job = run(start_and_wait(manager))

    payload = json.loads((tmp_path / "jobs.json").read_text())
    assert payload["jobs"][0]["status"] == "succeeded"
    assert payload["jobs"][0]["result"] == {
        "success": True,
        "username": "registered-user",
        "email": "temporary@example.test",
        "status": "completed",
        "duration": 1.25,
        "error_stage": None,
    }
    encoded = json.dumps(payload)
    assert "password" not in encoded
    assert "console_logs" not in encoded
    assert "token" not in encoded

    app = web_app.create_app(manager)
    with TestClient(app) as client:
        response = client.get(f"/api/jobs/{job.id}")
    assert response.status_code == 200
    assert response.json()["result"]["email"] == "temporary@example.test"
    assert "password" not in response.text
    assert "console_logs" not in response.text


def test_failure_is_persisted_without_secret_value(tmp_path, monkeypatch):
    async def fake_register(referral_code, phone, on_step_complete):
        assert re.fullmatch(r"\+1-\d{10}", phone)
        raise RuntimeError(
            "Authorization: Bearer secret-credential-not-for-output "
            "via http://proxy-user:proxy-password@example.test"
        )

    monkeypatch.setattr(web_app, "register_with_defaults", fake_register)
    manager = web_app.RegistrationManager(tmp_path / "jobs.json")
    run(start_and_wait(manager))

    persisted = json.loads((tmp_path / "jobs.json").read_text())["jobs"][0]
    assert persisted["status"] == "failed"
    assert "secret-credential-not-for-output" not in json.dumps(persisted)
    assert "proxy-password" not in json.dumps(persisted)
    assert "[redacted]" in persisted["error"]


def test_post_jobs_returns_conflict_while_task_is_active(tmp_path, monkeypatch):
    release = asyncio.Event()

    async def slow_register(referral_code, phone, on_step_complete):
        assert re.fullmatch(r"\+1-\d{10}", phone)
        await release.wait()
        return completed_result()

    monkeypatch.setattr(web_app, "register_with_defaults", slow_register)
    app = web_app.create_app(web_app.RegistrationManager(tmp_path / "jobs.json"))
    with TestClient(app) as client:
        first = client.post("/api/jobs")
        second = client.post("/api/jobs")
        assert first.status_code == 202
        assert first.headers["location"] == f"/api/jobs/{first.json()['id']}"
        assert second.status_code == 409
        task = app.state.registration_manager.get(first.json()["id"]).task
        task.cancel()


def test_restart_marks_running_job_failed_and_keeps_completed_history(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "running-job",
                        "status": "running",
                        "created_at": "2026-07-30T00:00:00+00:00",
                        "steps": [],
                        "result": None,
                        "error": None,
                    },
                    {
                        "id": "completed-job",
                        "status": "succeeded",
                        "created_at": "2026-07-30T00:00:00+00:00",
                        "steps": [],
                        "result": {
                            "success": True,
                            "username": "saved",
                            "email": "saved@example.test",
                        },
                        "error": None,
                    },
                ],
            }
        )
    )

    manager = web_app.RegistrationManager(path)
    run(manager.load())
    overview = manager.overview()
    states = {job["id"]: job for job in overview["jobs"]}
    assert states["running-job"]["status"] == "failed"
    assert states["running-job"]["error"] == web_app.RUNNING_INTERRUPTED_MESSAGE
    assert states["completed-job"]["status"] == "succeeded"
    assert json.loads(path.read_text())["jobs"][0]["status"] == "failed"


def test_routes_return_dashboard_health_and_overview_without_sensitive_fields(tmp_path):
    app = web_app.create_app(web_app.RegistrationManager(tmp_path / "jobs.json"))
    with TestClient(app) as client:
        dashboard = client.get("/")
        health = client.get("/health")
        overview = client.get("/api/overview")
        missing = client.get("/api/jobs/missing")

    assert dashboard.status_code == 200
    assert "2 秒" not in dashboard.text
    assert "setInterval(refresh, 2000)" in dashboard.text
    assert health.json() == {"ok": True, "active_job_id": None}
    assert overview.json() == {
        "active_job_id": None,
        "jobs": [],
        "total_jobs": 0,
        "successful_jobs": 0,
    }
    assert missing.status_code == 404
    assert all(
        field not in overview.text
        for field in ("password", "console_logs", "turnstile", "proxy_password")
    )


def test_generated_phone_matches_registration_api_format():
    for _ in range(20):
        assert re.fullmatch(r"\+1-\d{10}", web_app._generate_phone_number())
