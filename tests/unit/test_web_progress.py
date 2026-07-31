import asyncio

from fastapi.testclient import TestClient

from digitalplat_auto_register import web_app
from digitalplat_auto_register.core.account import (
    Account,
    AccountStatus,
    AccountStore,
    BatchRegistrationJob,
)
from digitalplat_auto_register.core.result import RegistrationResult, StepResult
from digitalplat_auto_register.types import RegistrationStatus


def test_batch_detail_exposes_all_account_registration_steps(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    account = Account(
        username="progress-user",
        email="",
        password="secret",
        status=AccountStatus.REGISTERING,
    )
    account.metadata = {
        "current_step": "browser_navigation",
        "steps": [
            {
                "name": "turnstile_token_acquisition",
                "success": True,
                "duration": 1.2,
                "message": "ok",
            },
            {
                "name": "email_creation",
                "success": True,
                "duration": 0.4,
                "message": "ok",
            },
        ],
    }
    store.create_account(account)
    batch = BatchRegistrationJob(
        status="running",
        total_accounts=1,
        account_ids=[account.id],
    )
    store.create_batch_job(batch)
    manager = web_app.RegistrationManager(store, tmp_path / "jobs.json")

    with TestClient(web_app.create_app(manager, store)) as client:
        response = client.get(f"/api/batch/{batch.id}")

    assert response.status_code == 200
    progress = response.json()["accounts"][0]["progress"]
    assert progress["current_step"] == "browser_navigation"
    assert progress["completed_steps"] == 2
    assert progress["total_steps"] == 6
    assert [step["name"] for step in progress["steps"]] == list(
        web_app.REGISTRATION_STEP_ORDER
    )


def test_legacy_account_progress_renderer_is_kept_for_api_compatibility(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    manager = web_app.RegistrationManager(store, tmp_path / "jobs.json")

    with TestClient(web_app.create_app(manager, store)) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "DigitalPlat 域名自动注册" in response.text
    assert "前缀订阅" in response.text
    assert "function accountProgressRow" in web_app.DASHBOARD_HTML
    assert "function stepTimeline" in web_app.DASHBOARD_HTML


def test_batch_counts_accounts_only_after_registration_finishes(tmp_path, monkeypatch):
    async def fake_register(**kwargs):
        callback = kwargs["on_step_complete"]
        for step_name in web_app.REGISTRATION_STEP_ORDER:
            callback(
                StepResult(
                    name=step_name,
                    status="completed",
                    success=True,
                    duration=0.1,
                    message="ok",
                )
            )
        return RegistrationResult(
            success=True,
            username=kwargs["username"],
            email="registered@example.test",
            password=kwargs["password"],
            registration_status=RegistrationStatus.COMPLETED,
        )

    async def run_batch():
        store = AccountStore(tmp_path / "accounts.json")
        await store.load()
        manager = web_app.RegistrationManager(store, tmp_path / "jobs.json")
        batch = await manager.start_batch(count=2, delay=0, max_concurrent=2)
        await manager._batch_task
        return manager, store, batch

    monkeypatch.setattr(web_app, "register_with_defaults", fake_register)
    manager, store, batch = asyncio.run(run_batch())

    assert batch.status == "completed"
    assert batch.completed_accounts == 2
    assert batch.successful_accounts == 2
    for account_id in batch.account_ids:
        progress = manager.account_progress(store.get_account(account_id))
        assert progress["completed_steps"] == 6
        assert progress["current_step"] is None
