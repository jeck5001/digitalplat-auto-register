import asyncio

from fastapi.testclient import TestClient

from digitalplat_auto_register.core.account import AccountStore
from digitalplat_auto_register.core.domain_automation import (
    APITokenRecord,
    CloudflareSettings,
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

    def update_nameservers(self, domain, nameservers):
        self.inventory.setdefault(self.token, []).append({
            "name": domain,
            "nameservers": nameservers,
        })
        return {"name": domain, "nameservers": nameservers}

    def renew_domain(self, domain, renewal_type, years):
        for item in self.inventory.setdefault(self.token, []):
            if str(item.get("name") or item.get("domain")) == domain:
                item["expiry_date"] = "2030-01-01"
                item["renewal_type"] = renewal_type
                item["renewal_years"] = years
                return item
        raise DigitalPlatAPIError("domain not found", 404)


class FakeCloudflareClient:
    zones = {}

    def __init__(self, api_token, account_id, api_base):
        self.api_token = api_token
        self.account_id = account_id

    def verify_token(self):
        return {"status": "active"}

    def find_zone(self, domain):
        return self.zones.get(domain)

    def create_zone(self, domain):
        zone = {
            "id": f"zone-{domain}",
            "name": domain,
            "status": "pending",
            "name_servers": ["alice.ns.cloudflare.com", "bob.ns.cloudflare.com"],
        }
        self.zones[domain] = zone
        return zone

    def get_zone(self, zone_id):
        domain = zone_id.removeprefix("zone-")
        return self.zones[domain]


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
        job = await manager.start_job(subscription.id, target_count=1, max_attempts=3, delay_min_seconds=0, delay_max_seconds=0)
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
        job = await manager.start_job(subscription.id, target_count=1, max_attempts=3, delay_min_seconds=0, delay_max_seconds=0)
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


def test_cloudflare_subscription_allows_empty_nameservers():
    subscription = DomainAutomationManager.normalize_subscription({
        "name": "Cloudflare",
        "prefix": "app",
        "suffix": "dpdns.org",
        "slot_type": "free",
        "random_length": 6,
        "auto_cloudflare": True,
        "nameservers": [],
    })

    assert subscription["auto_cloudflare"] is True
    assert subscription["nameservers"] == []


def test_existing_domain_can_be_hosted_on_cloudflare(tmp_path):
    FakeDomainClient.inventory = {}
    FakeCloudflareClient.zones = {}
    store, unused, subscription = build_manager(tmp_path)
    store.cloudflare = CloudflareSettings(
        account_id="0123456789abcdef0123456789abcdef",
        api_token="cloudflare_test_token_123456789",
    )
    token = next(iter(store.tokens.values()))
    store.domains.append({
        "domain": "app123.dpdns.org",
        "token_id": token.id,
        "token_name": token.name,
        "subscription_id": subscription.id,
        "slot_type": "free",
        "nameservers": [],
        "status": "ok",
    })
    manager = DomainAutomationManager(
        store,
        FakeDomainClient,
        FakeCloudflareClient,
    )
    manager.cloudflare_delay_min = 0
    manager.cloudflare_delay_max = 0

    record = asyncio.run(manager.host_domain_on_cloudflare("app123.dpdns.org"))

    assert record["cloudflare_status"] == "pending"
    assert record["nameservers"] == [
        "alice.ns.cloudflare.com",
        "bob.ns.cloudflare.com",
    ]
    assert [step["status"] for step in record["cloudflare_steps"]] == [
        "success",
        "success",
        "success",
        "pending",
    ]


def test_cloudflare_web_api_masks_token_and_hosts_domain(tmp_path):
    account_store = AccountStore(tmp_path / "accounts.json")
    registration_manager = RegistrationManager(account_store, tmp_path / "jobs.json")
    domain_store, unused, subscription = build_manager(tmp_path)
    token = next(iter(domain_store.tokens.values()))
    domain_store.domains.append({
        "domain": "web123.dpdns.org",
        "token_id": token.id,
        "token_name": token.name,
        "subscription_id": subscription.id,
        "slot_type": "free",
        "nameservers": [],
        "status": "ok",
    })
    domain_manager = DomainAutomationManager(
        domain_store,
        FakeDomainClient,
        FakeCloudflareClient,
    )
    domain_manager.cloudflare_delay_min = 0
    domain_manager.cloudflare_delay_max = 0
    app = create_app(
        registration_manager,
        account_store,
        domain_store,
        domain_manager,
    )

    with TestClient(app) as client:
        saved = client.put("/api/domain-automation/cloudflare", json={
            "account_id": "0123456789abcdef0123456789abcdef",
            "api_token": "cloudflare_test_token_123456789",
        })
        hosted = client.post(
            "/api/domain-automation/domains/web123.dpdns.org/cloudflare"
        )
        overview = client.get("/api/domain-automation")

    assert saved.status_code == 200
    assert "cloudflare_test_token_123456789" not in saved.text
    assert hosted.status_code == 200
    assert overview.json()["cloudflare"]["token_masked"].startswith("cloudfla")


def test_sync_domains_imports_domains_from_each_enabled_token(tmp_path):
    FakeDomainClient.inventory = {
        "dp_test_token_a_123456": [{
            "name": "synced.dpdns.org",
            "status": "ok",
            "nameservers": ["alice.ns.cloudflare.com", "bob.ns.cloudflare.com"],
        }],
    }
    store, manager, unused = build_manager(tmp_path)
    result = asyncio.run(manager.sync_domains())

    assert result["synced"] == 1
    assert store.domains[0]["domain"] == "synced.dpdns.org"
    assert store.domains[0]["source"] == "digitalplat_sync"


def test_renewal_renews_only_domains_inside_window(tmp_path):
    from datetime import datetime, timedelta

    now = datetime.now().astimezone()
    FakeDomainClient.inventory = {
        "dp_test_token_a_123456": [
            {
                "name": "soon.dpdns.org",
                "expiry_date": (now + timedelta(days=30)).date().isoformat(),
                "status": "ok",
            },
            {
                "name": "later.dpdns.org",
                "expiry_date": (now + timedelta(days=300)).date().isoformat(),
                "status": "ok",
            },
        ],
    }
    store, manager, unused = build_manager(tmp_path)
    store.renewal.renew_before_days = 120
    store.renewal.delay_min_seconds = 0
    store.renewal.delay_max_seconds = 0

    result = asyncio.run(manager.run_renewal())

    assert result["checked"] == 2
    assert result["renewed"] == 1
    assert result["skipped"] == 1
    records = {item["domain"]: item for item in store.domains}
    assert records["soon.dpdns.org"]["renewal_status"] == "renewed"
    assert records["later.dpdns.org"]["renewal_status"] == "skipped"


def test_renewal_web_api_saves_configuration(tmp_path):
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
        response = client.put("/api/domain-automation/renewal", json={
            "enabled": True,
            "renew_before_days": 90,
            "renewal_type": "free",
            "renewal_years": 1,
            "interval_seconds": 86400,
            "delay_min_seconds": 2,
            "delay_max_seconds": 4,
        })
        overview = client.get("/api/domain-automation")

    assert response.status_code == 200
    assert overview.json()["renewal"]["renew_before_days"] == 90
    assert overview.json()["renewal"]["delay_max_seconds"] == 4.0
