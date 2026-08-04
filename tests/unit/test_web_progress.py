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


def test_original_console_and_new_domain_module_have_separate_routes(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    manager = web_app.RegistrationManager(store, tmp_path / "jobs.json")

    with TestClient(web_app.create_app(manager, store)) as client:
        original_console = client.get("/")
        domain_console = client.get("/domain-automation")

    assert original_console.status_code == 200
    assert "DigitalPlat 控制台" in original_console.text
    assert "批量注册" in original_console.text
    assert "账号管理" in original_console.text
    assert "账号域名注册" in original_console.text
    assert 'href="/domain-automation"' in original_console.text
    assert "/static/js/console.js" in original_console.text
    assert "/static/css/app.css" in original_console.text

    assert domain_console.status_code == 200
    assert "DigitalPlat 域名自动注册" in domain_console.text
    assert "前缀订阅" in domain_console.text
    assert 'href="/"' in domain_console.text
    assert "/static/js/domain_automation.js" in domain_console.text


def test_original_account_domain_routes_are_kept(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    account = Account(
        username="domain-owner",
        email="owner@example.test",
        password="secret",
        status=AccountStatus.ACTIVE,
    )
    account.metadata["domains"] = [{
        "domain": "existing.dpdns.org",
        "registered_at": "2026-07-31T12:00:00+08:00",
        "nameservers": ["ns1.example.test", "ns2.example.test"],
    }]
    store.create_account(account)
    manager = web_app.RegistrationManager(store, tmp_path / "jobs.json")

    with TestClient(web_app.create_app(manager, store)) as client:
        response = client.get("/api/domains")
        invalid_check = client.post("/api/domains/check", json={})
        invalid_registration = client.post("/api/domains/register", json={})

    assert response.status_code == 200
    assert response.json()["domains"][0]["domain"] == "existing.dpdns.org"
    assert invalid_check.status_code == 400
    assert invalid_registration.status_code == 400


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
        batch = await manager.start_batch(count=2, delay=0, delay_max=0, max_concurrent=2)
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


def test_batch_honors_concurrency_with_positive_start_delay(tmp_path, monkeypatch):
    active = 0
    maximum_active = 0
    two_started = asyncio.Event()

    async def fake_register(**kwargs):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        if active == 2:
            two_started.set()
        try:
            await asyncio.wait_for(two_started.wait(), timeout=0.5)
            return RegistrationResult(
                success=True,
                username=kwargs["username"],
                email="registered@example.test",
                password=kwargs["password"],
                registration_status=RegistrationStatus.COMPLETED,
            )
        finally:
            active -= 1

    async def run_batch():
        store = AccountStore(tmp_path / "accounts.json")
        await store.load()
        manager = web_app.RegistrationManager(store, tmp_path / "jobs.json")
        batch = await manager.start_batch(
            count=3,
            delay=0.01,
            delay_max=0.01,
            max_concurrent=2,
        )
        await manager._batch_task
        return batch

    monkeypatch.setattr(web_app, "register_with_defaults", fake_register)
    batch = asyncio.run(run_batch())

    assert maximum_active == 2
    assert batch.status == "completed"
    assert batch.successful_accounts == 3
