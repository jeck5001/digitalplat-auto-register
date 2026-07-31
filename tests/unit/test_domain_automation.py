import asyncio

from fastapi.testclient import TestClient

from digitalplat_auto_register.core.account import AccountStore
from digitalplat_auto_register.core.domain_automation import (
    APITokenRecord,
    DEFAULT_USER_AGENT,
    DigitalPlatAPIError,
    DigitalPlatDomainClient,
    DomainAutomationManager,
    DomainAutomationStore,
    PrefixSubscription,
)
from digitalplat_auto_register.web_app import RegistrationManager, create_app


class FakeDomainClient:
    attempts = []
    inventory = {}
    ambiguous_domains = set()

    def __init__(self, token, api_base):
        self.token = token

    def list_domains(self):
        return list(self.inventory.get(self.token, []))

    def register_domain(self, domain, slot_type, nameservers):
        self.attempts.append((self.token, domain, slot_type, nameservers))
        if domain in self.ambiguous_domains:
            self.inventory.setdefault(self.token, []).append({"name": domain, "status": "ok"})
            raise DigitalPlatAPIError("network timeout", ambiguous=True)
        if len(self.attempts) == 1:
            raise DigitalPlatAPIError("DigitalPlat HTTP 409: domain unavailable", 409)
        return {
            "name": domain,
            "status": "ok",
            "slot_type": slot_type,
            "lifecycle_type": slot_type,
        }


def build_manager(tmp_path):
    store = DomainAutomationStore(tmp_path / "domain-automation.json")
    token_a = APITokenRecord(name="Token A", token="dp_test_token_a_123456")
    token_b = APITokenRecord(name="Token B", token="dp_test_token_b_123456")
    store.tokens[token_a.id] = token_a
    store.tokens[token_b.id] = token_b
    subscription = PrefixSubscription(
        name="Blog",
        prefix="blog",
        suffix="us.kg",
        nameservers=["ns1.provider.com", "ns2.provider.com"],
        random_length=4,
    )
    store.subscriptions[subscription.id] = subscription
    return store, DomainAutomationManager(store, FakeDomainClient), subscription


def test_multi_token_job_changes_candidate_after_registration_conflict(tmp_path):
    FakeDomainClient.attempts = []
    FakeDomainClient.inventory = {}
    FakeDomainClient.ambiguous_domains = set()
    store, manager, subscription = build_manager(tmp_path)

    async def run_job():
        job = await manager.start_job(subscription.id, target_count=1, max_attempts=3)
        await manager.tasks[job.id]
        return job

    job = asyncio.run(run_job())

    assert job.status == "completed"
    assert job.successful_domains == 1
    assert job.failed_attempts == 1
    assert len(FakeDomainClient.attempts) == 2
    assert FakeDomainClient.attempts[0][0] != FakeDomainClient.attempts[1][0]
    assert FakeDomainClient.attempts[0][1] != FakeDomainClient.attempts[1][1]
    assert store.domains[0]["domain"] == FakeDomainClient.attempts[1][1]


def test_ambiguous_registration_is_reconciled_without_retry(tmp_path):
    FakeDomainClient.attempts = []
    FakeDomainClient.inventory = {}
    store, manager, subscription = build_manager(tmp_path)
    fixed_domain = "blog-safe.us.kg"
    manager._candidate = lambda unused: fixed_domain
    FakeDomainClient.ambiguous_domains = {fixed_domain}

    async def run_job():
        job = await manager.start_job(subscription.id, target_count=1, max_attempts=3)
        await manager.tasks[job.id]
        return job

    job = asyncio.run(run_job())

    assert job.status == "completed"
    assert job.successful_domains == 1
    assert len(FakeDomainClient.attempts) == 1
    assert job.attempts[0].steps[-1]["status"] == "success"
    assert "reconciliation" in job.attempts[0].steps[-1]["message"]


def test_web_api_never_returns_raw_tokens_and_serves_domain_console(tmp_path):
    account_store = AccountStore(tmp_path / "accounts.json")
    registration_manager = RegistrationManager(account_store, tmp_path / "jobs.json")
    domain_store, domain_manager, unused = build_manager(tmp_path)
    app = create_app(
        registration_manager,
        account_store,
        domain_store,
        domain_manager,
    )

    with TestClient(app) as client:
        overview = client.get("/api/domain-automation")
        dashboard = client.get("/domain-automation")

    assert overview.status_code == 200
    assert "dp_test_token_a_123456" not in overview.text
    assert overview.json()["tokens"][0]["token_masked"].startswith("dp_test_")
    assert dashboard.status_code == 200
    assert "DigitalPlat 域名自动注册" in dashboard.text
    assert "前缀订阅" in dashboard.text
    assert "Turnstile 配置" not in dashboard.text
    assert "批量创建账号" not in dashboard.text


def test_token_and_subscription_validation():
    assert DomainAutomationManager.validate_token("dp_live_example_123") == "dp_live_example_123"
    subscription = DomainAutomationManager.normalize_subscription({
        "name": "Blog",
        "prefix": "blog",
        "suffix": ".us.kg",
        "slot_type": "subscription",
        "random_length": 6,
        "separator": "-",
        "nameservers": ["ns1.provider.com", "ns2.provider.com"],
    })
    assert subscription["suffix"] == "us.kg"
    assert subscription["slot_type"] == "subscription"


def test_api_client_uses_browser_compatible_headers():
    client = DigitalPlatDomainClient("dp_test_example_123456")

    assert client.headers["User-Agent"] == DEFAULT_USER_AGENT
    assert client.headers["Accept"] == "application/json, text/plain, */*"


def test_dpdns_candidates_never_use_hyphen_separator(tmp_path):
    store = DomainAutomationStore(tmp_path / "domain-automation.json")
    manager = DomainAutomationManager(store, FakeDomainClient)
    subscription = PrefixSubscription(
        name="DPDNS",
        prefix="guagua",
        suffix="dpdns.org",
        nameservers=["ns1.provider.com", "ns2.provider.com"],
        random_length=6,
        separator="-",
    )

    candidate = manager._candidate(subscription)

    assert candidate.startswith("guagua")
    assert not candidate.startswith("guagua-")
    assert candidate.endswith(".dpdns.org")


def test_dpdns_subscription_normalization_removes_hyphen():
    subscription = DomainAutomationManager.normalize_subscription({
        "name": "DPDNS",
        "prefix": "guagua",
        "suffix": "dpdns.org",
        "slot_type": "free",
        "random_length": 6,
        "separator": "-",
        "nameservers": ["ns1.provider.com", "ns2.provider.com"],
    })

    assert subscription["separator"] == ""
