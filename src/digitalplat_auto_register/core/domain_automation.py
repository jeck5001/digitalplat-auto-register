"""Persistent multi-token domain registration automation."""

import asyncio
import functools
import json
import os
import random
import re
import string
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote
from uuid import uuid4

import requests


DEFAULT_API_BASE = "https://domain-api.digitalplat.org/api/v1"
DEFAULT_CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"
DEFAULT_DATA_PATH = "/app/data/domain-automation.json"
DEFAULT_SEPARATOR = ""
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
TOKEN_PATTERN = re.compile(r"^dp_(?:live|test)_[A-Za-z0-9_-]+$")
LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
HOSTNAME_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
DOMAIN_STEP_ORDER = (
    "candidate_generation",
    "token_assignment",
    "registration_request",
    "registration_verification",
)
DOMAIN_STEP_LABELS = {
    "candidate_generation": "生成候选域名",
    "token_assignment": "分配 API Token",
    "registration_request": "提交注册请求",
    "registration_verification": "确认注册结果",
}
CLOUDFLARE_STEP_ORDER = (
    "zone_provisioning",
    "nameserver_assignment",
    "digitalplat_delegation",
    "cloudflare_activation",
)
CLOUDFLARE_STEP_LABELS = {
    "zone_provisioning": "创建 Cloudflare Zone",
    "nameserver_assignment": "获取专属 Nameservers",
    "digitalplat_delegation": "更新 DigitalPlat Nameservers",
    "cloudflare_activation": "确认 Cloudflare 托管状态",
}
DEFAULT_RENEW_BEFORE_DAYS = 120
DEFAULT_RENEWAL_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_RENEWAL_DELAY_MIN_SECONDS = 3.0
DEFAULT_RENEWAL_DELAY_MAX_SECONDS = 6.0


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _mask_token(token: str) -> str:
    if len(token) <= 12:
        return "***"
    return f"{token[:8]}...{token[-4:]}"


def _safe_error(value: Any, limit: int = 800) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


async def _run_sync(function: Callable[..., Any], *args: Any) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(function, *args))


class DigitalPlatAPIError(RuntimeError):
    """A safe API failure that never contains the bearer token."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.ambiguous = ambiguous


class CloudflareAPIError(RuntimeError):
    """A safe Cloudflare API failure that never contains credentials."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class CloudflareClient:
    """Minimal client for Cloudflare Zone provisioning and status checks."""

    def __init__(
        self,
        api_token: str,
        account_id: str,
        api_base: str = DEFAULT_CLOUDFLARE_API_BASE,
        timeout: float = 30.0,
    ) -> None:
        self.account_id = account_id
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
        }

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        try:
            response = requests.request(
                method,
                f"{self.api_base}{path}",
                headers=self.headers,
                json=payload,
                params=params,
                timeout=self.timeout,
            )
        except requests.RequestException as error:
            raise CloudflareAPIError(
                f"Cloudflare network error: {error.__class__.__name__}"
            ) from error

        try:
            body = response.json() if response.content else {}
        except ValueError as error:
            raise CloudflareAPIError(
                f"Cloudflare returned HTTP {response.status_code} with non-JSON content",
                response.status_code,
            ) from error

        if not response.ok or not isinstance(body, dict) or body.get("success") is not True:
            errors = body.get("errors", []) if isinstance(body, dict) else []
            messages = [
                str(item.get("message"))
                for item in errors
                if isinstance(item, dict) and item.get("message")
            ]
            message = "; ".join(messages) or f"HTTP {response.status_code}"
            raise CloudflareAPIError(
                f"Cloudflare API error: {_safe_error(message)}",
                response.status_code,
            )
        return body.get("result")

    def verify_token(self) -> Dict[str, Any]:
        result = self._request("GET", "/user/tokens/verify")
        if not isinstance(result, dict):
            raise CloudflareAPIError("Cloudflare token verification returned an unexpected shape")
        return result

    def find_zone(self, domain: str) -> Optional[Dict[str, Any]]:
        result = self._request(
            "GET",
            "/zones",
            params={"name": domain, "account.id": self.account_id, "per_page": 50},
        )
        if not isinstance(result, list):
            raise CloudflareAPIError("Cloudflare zone list returned an unexpected shape")
        for zone in result:
            if isinstance(zone, dict) and str(zone.get("name", "")).lower() == domain.lower():
                return zone
        return None

    def create_zone(self, domain: str) -> Dict[str, Any]:
        result = self._request(
            "POST",
            "/zones",
            {
                "name": domain,
                "account": {"id": self.account_id},
                "type": "full",
                "jump_start": False,
            },
        )
        if not isinstance(result, dict):
            raise CloudflareAPIError("Cloudflare zone creation returned an unexpected shape")
        return result

    def get_zone(self, zone_id: str) -> Dict[str, Any]:
        result = self._request("GET", f"/zones/{quote(zone_id, safe='')}")
        if not isinstance(result, dict):
            raise CloudflareAPIError("Cloudflare zone status returned an unexpected shape")
        return result


class DigitalPlatDomainClient:
    """Small client for the documented DigitalPlat Domains API."""

    def __init__(
        self,
        token: str,
        api_base: str = DEFAULT_API_BASE,
        timeout: float = 30.0,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            # Cloudflare challenges non-browser User-Agents before the API
            # receives the bearer token, returning an HTML 403 page.
            "User-Agent": DEFAULT_USER_AGENT,
        }

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        mutation: bool = False,
    ) -> Any:
        try:
            response = requests.request(
                method,
                f"{self.api_base}{path}",
                headers=self.headers,
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as error:
            raise DigitalPlatAPIError(
                f"DigitalPlat network error: {error.__class__.__name__}",
                ambiguous=mutation,
            ) from error

        try:
            body = response.json() if response.content else {}
        except ValueError as error:
            server = str(response.headers.get("server", "")).lower()
            challenge = response.status_code == 403 and (
                "cloudflare" in server or "challenge page" in response.text.lower()
            )
            message = (
                "DigitalPlat API request was blocked by a Cloudflare challenge"
                if challenge
                else f"DigitalPlat returned HTTP {response.status_code} with non-JSON content"
            )
            raise DigitalPlatAPIError(
                message,
                status_code=response.status_code,
                ambiguous=mutation and response.status_code >= 500,
            ) from error

        if not response.ok or (isinstance(body, dict) and body.get("success") is False):
            message: Any = body
            if isinstance(body, dict):
                message = body.get("error") or body.get("message") or body
                if isinstance(message, dict):
                    message = message.get("message") or message.get("error") or message
            raise DigitalPlatAPIError(
                f"DigitalPlat HTTP {response.status_code}: {_safe_error(message)}",
                status_code=response.status_code,
                ambiguous=mutation and response.status_code >= 500,
            )

        return body.get("data", body) if isinstance(body, dict) else body

    def list_domains(self) -> List[Dict[str, Any]]:
        payload = self._request("GET", "/domains")
        if isinstance(payload, dict):
            payload = payload.get("domains", [])
        if not isinstance(payload, list):
            raise DigitalPlatAPIError("DigitalPlat domain list has an unexpected shape")
        return [item for item in payload if isinstance(item, dict)]

    def get_domain(self, domain: str) -> Dict[str, Any]:
        payload = self._request("GET", f"/domains/{quote(domain, safe='')}")
        if isinstance(payload, dict):
            payload = payload.get("domain", payload)
        if not isinstance(payload, dict):
            raise DigitalPlatAPIError("DigitalPlat domain detail has an unexpected shape")
        return payload

    def register_domain(
        self,
        domain: str,
        slot_type: str,
        nameservers: List[str],
    ) -> Dict[str, Any]:
        payload = self._request(
            "POST",
            "/domains",
            {
                "domain": domain,
                "slot_type": slot_type,
                "nameservers": nameservers,
            },
            mutation=True,
        )
        if not isinstance(payload, dict):
            raise DigitalPlatAPIError("DigitalPlat registration response has an unexpected shape")
        return payload

    def update_nameservers(self, domain: str, nameservers: List[str]) -> Dict[str, Any]:
        payload = self._request(
            "PATCH",
            f"/domains/{quote(domain, safe='')}/nameservers",
            {"nameservers": nameservers},
            mutation=True,
        )
        if not isinstance(payload, dict):
            raise DigitalPlatAPIError("DigitalPlat nameserver response has an unexpected shape")
        return payload

    def renew_domain(self, domain: str, renewal_type: str, years: int) -> Dict[str, Any]:
        payload = self._request(
            "POST",
            f"/domains/{quote(domain, safe='')}/renew",
            {"renewal_type": renewal_type, "years": years},
            mutation=True,
        )
        if isinstance(payload, dict):
            payload = payload.get("domain", payload)
        if not isinstance(payload, dict):
            raise DigitalPlatAPIError("DigitalPlat renewal response has an unexpected shape")
        return payload


@dataclass
class APITokenRecord:
    name: str
    token: str
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    enabled: bool = True
    created_at: str = field(default_factory=_timestamp)
    last_checked_at: Optional[str] = None
    last_status: str = "untested"
    last_error: Optional[str] = None
    domain_count: Optional[int] = None

    def safe_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "token_masked": _mask_token(self.token),
            "environment": "test" if self.token.startswith("dp_test_") else "live",
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_checked_at": self.last_checked_at,
            "last_status": self.last_status,
            "last_error": _safe_error(self.last_error),
            "domain_count": self.domain_count,
        }


@dataclass
class PrefixSubscription:
    name: str
    prefix: str
    suffix: str
    nameservers: List[str]
    slot_type: str = "subscription"
    random_length: int = 6
    separator: str = DEFAULT_SEPARATOR
    auto_cloudflare: bool = False
    enabled: bool = True
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    created_at: str = field(default_factory=_timestamp)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CloudflareSettings:
    account_id: str
    api_token: str
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    enabled: bool = True
    last_checked_at: Optional[str] = None
    last_status: str = "untested"
    last_error: Optional[str] = None

    def safe_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "account_id": self.account_id,
            "token_masked": _mask_token(self.api_token),
            "enabled": self.enabled,
            "last_checked_at": self.last_checked_at,
            "last_status": self.last_status,
            "last_error": _safe_error(self.last_error),
        }


@dataclass
class RenewalSettings:
    enabled: bool = True
    renew_before_days: int = DEFAULT_RENEW_BEFORE_DAYS
    renewal_type: str = "free"
    renewal_years: int = 1
    interval_seconds: int = DEFAULT_RENEWAL_INTERVAL_SECONDS
    delay_min_seconds: float = DEFAULT_RENEWAL_DELAY_MIN_SECONDS
    delay_max_seconds: float = DEFAULT_RENEWAL_DELAY_MAX_SECONDS
    last_run_at: Optional[str] = None
    last_status: str = "untested"
    last_error: Optional[str] = None
    last_summary: Optional[Dict[str, Any]] = None

    def safe_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DomainAttempt:
    domain: str
    token_id: str
    token_name: str
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    status: str = "running"
    created_at: str = field(default_factory=_timestamp)
    finished_at: Optional[str] = None
    steps: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    response: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DomainRegistrationJob:
    subscription_id: str
    target_count: int
    token_ids: List[str]
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    status: str = "pending"
    created_at: str = field(default_factory=_timestamp)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    completed_attempts: int = 0
    successful_domains: int = 0
    failed_attempts: int = 0
    max_attempts: int = 0
    delay_min_seconds: float = 20.0
    delay_max_seconds: float = 45.0
    attempts: List[DomainAttempt] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["attempts"] = [attempt.to_dict() for attempt in self.attempts]
        return data


class DomainAutomationStore:
    """Persist tokens, prefix subscriptions, jobs, and successful domains."""

    def __init__(self, data_path: Optional[Path] = None) -> None:
        self.data_path = Path(data_path or os.getenv("DOMAIN_AUTOMATION_PATH", DEFAULT_DATA_PATH))
        self.tokens: Dict[str, APITokenRecord] = {}
        self.subscriptions: Dict[str, PrefixSubscription] = {}
        self.jobs: Dict[str, DomainRegistrationJob] = {}
        self.domains: List[Dict[str, Any]] = []
        self.cloudflare: Optional[CloudflareSettings] = None
        self.renewal = RenewalSettings()
        self.loaded = False

    async def load(self) -> None:
        if self.loaded:
            return
        self.loaded = True
        if not self.data_path.exists():
            return
        try:
            payload = json.loads(self.data_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for raw in payload.get("tokens", []):
            record = APITokenRecord(**{
                key: value for key, value in raw.items()
                if key in APITokenRecord.__dataclass_fields__
            })
            self.tokens[record.id] = record
        for raw in payload.get("subscriptions", []):
            subscription = PrefixSubscription(**{
                key: value for key, value in raw.items()
                if key in PrefixSubscription.__dataclass_fields__
            })
            self.subscriptions[subscription.id] = subscription
        raw_cloudflare = payload.get("cloudflare")
        if isinstance(raw_cloudflare, dict) and raw_cloudflare.get("api_token") and raw_cloudflare.get("account_id"):
            self.cloudflare = CloudflareSettings(**{
                key: value for key, value in raw_cloudflare.items()
                if key in CloudflareSettings.__dataclass_fields__
            })
        raw_renewal = payload.get("renewal")
        if isinstance(raw_renewal, dict):
            self.renewal = RenewalSettings(**{
                key: value for key, value in raw_renewal.items()
                if key in RenewalSettings.__dataclass_fields__
            })
        for raw in payload.get("jobs", []):
            attempts = [DomainAttempt(**attempt) for attempt in raw.pop("attempts", [])]
            job = DomainRegistrationJob(**{
                key: value for key, value in raw.items()
                if key in DomainRegistrationJob.__dataclass_fields__
            })
            job.attempts = attempts
            if job.status in {"pending", "running"}:
                job.status = "failed"
                job.error = "Task interrupted by service restart"
                job.finished_at = _timestamp()
            self.jobs[job.id] = job
        self.domains = [item for item in payload.get("domains", []) if isinstance(item, dict)]

    def _persist(self) -> None:
        self.data_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "tokens": [asdict(item) for item in self.tokens.values()],
            "subscriptions": [item.to_dict() for item in self.subscriptions.values()],
            "jobs": [item.to_dict() for item in self.jobs.values()],
            "domains": self.domains,
            "cloudflare": asdict(self.cloudflare) if self.cloudflare else None,
            "renewal": asdict(self.renewal),
            "saved_at": _timestamp(),
        }
        fd, temporary_path = tempfile.mkstemp(
            prefix=".domain-automation-",
            suffix=".json",
            dir=str(self.data_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.data_path)
        except Exception:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)
            raise

    async def save(self) -> None:
        await _run_sync(self._persist)


class DomainAutomationManager:
    """Coordinate candidate generation and safe multi-token registration."""

    def __init__(
        self,
        store: DomainAutomationStore,
        client_factory: Callable[..., DigitalPlatDomainClient] = DigitalPlatDomainClient,
        cloudflare_client_factory: Callable[..., CloudflareClient] = CloudflareClient,
    ) -> None:
        self.store = store
        self.client_factory = client_factory
        self.cloudflare_client_factory = cloudflare_client_factory
        self.tasks: Dict[str, asyncio.Task] = {}
        self.api_base = os.getenv("DIGITALPLAT_API_BASE", DEFAULT_API_BASE)
        self.cloudflare_api_base = os.getenv(
            "CLOUDFLARE_API_BASE", DEFAULT_CLOUDFLARE_API_BASE
        )
        self.cloudflare_delay_min = max(0.0, float(os.getenv("CLOUDFLARE_OPERATION_DELAY_MIN", "3")))
        self.cloudflare_delay_max = max(
            self.cloudflare_delay_min,
            float(os.getenv("CLOUDFLARE_OPERATION_DELAY_MAX", "8")),
        )
        self._cloudflare_last_started = 0.0
        self._cloudflare_lock = asyncio.Lock()
        self.renewal_task: Optional[asyncio.Task] = None
        self._renewal_lock = asyncio.Lock()

    async def _wait_cloudflare_operation(self) -> None:
        async with self._cloudflare_lock:
            now = asyncio.get_running_loop().time()
            if self._cloudflare_last_started:
                delay = random.uniform(self.cloudflare_delay_min, self.cloudflare_delay_max)
                remaining = delay - (now - self._cloudflare_last_started)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            self._cloudflare_last_started = asyncio.get_running_loop().time()

    @staticmethod
    def validate_token(token: str) -> str:
        value = str(token or "").strip()
        if not TOKEN_PATTERN.fullmatch(value):
            raise ValueError("Token must start with dp_live_ or dp_test_")
        return value

    @staticmethod
    def normalize_cloudflare_settings(request: Dict[str, Any]) -> Dict[str, str]:
        account_id = str(request.get("account_id", "")).strip()
        api_token = str(request.get("api_token", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", account_id):
            raise ValueError("Cloudflare Account ID is invalid")
        if len(api_token) < 20 or any(character.isspace() for character in api_token):
            raise ValueError("Cloudflare API Token is invalid")
        return {"account_id": account_id, "api_token": api_token}

    @staticmethod
    def normalize_renewal_settings(request: Dict[str, Any]) -> Dict[str, Any]:
        def integer(name: str, default: int, minimum: int, maximum: int) -> int:
            value = request.get(name, default)
            if isinstance(value, bool):
                raise ValueError(f"{name} must be an integer")
            try:
                value = int(value)
            except (TypeError, ValueError) as error:
                raise ValueError(f"{name} must be an integer") from error
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
            return value

        def decimal(name: str, default: float) -> float:
            try:
                value = float(request.get(name, default))
            except (TypeError, ValueError) as error:
                raise ValueError(f"{name} must be a number") from error
            if value < 0 or value > 3600:
                raise ValueError(f"{name} must be between 0 and 3600")
            return value

        renewal_type = str(request.get("renewal_type", "free")).strip().lower()
        if renewal_type not in {"free", "paid"}:
            raise ValueError("renewal_type must be free or paid")
        delay_min = decimal("delay_min_seconds", DEFAULT_RENEWAL_DELAY_MIN_SECONDS)
        delay_max = decimal("delay_max_seconds", DEFAULT_RENEWAL_DELAY_MAX_SECONDS)
        if delay_max < delay_min:
            raise ValueError("delay_max_seconds must be greater than or equal to delay_min_seconds")
        return {
            "enabled": bool(request.get("enabled", True)),
            "renew_before_days": integer("renew_before_days", DEFAULT_RENEW_BEFORE_DAYS, 0, 3650),
            "renewal_type": renewal_type,
            "renewal_years": integer("renewal_years", 1, 1, 5),
            "interval_seconds": integer("interval_seconds", DEFAULT_RENEWAL_INTERVAL_SECONDS, 60, 31_536_000),
            "delay_min_seconds": delay_min,
            "delay_max_seconds": delay_max,
        }

    def _cloudflare_client(self) -> CloudflareClient:
        settings = self.store.cloudflare
        if not settings or not settings.enabled:
            raise ValueError("Cloudflare configuration is missing or disabled")
        return self.cloudflare_client_factory(
            settings.api_token,
            settings.account_id,
            self.cloudflare_api_base,
        )

    async def test_cloudflare(self) -> Dict[str, Any]:
        settings = self.store.cloudflare
        if not settings:
            raise ValueError("Cloudflare configuration is missing")
        try:
            await _run_sync(self._cloudflare_client().verify_token)
            settings.last_status = "valid"
            settings.last_error = None
        except CloudflareAPIError as error:
            settings.last_status = "invalid"
            settings.last_error = _safe_error(error)
        settings.last_checked_at = _timestamp()
        await self.store.save()
        return settings.safe_dict()

    @staticmethod
    def _parse_expiry(value: Any) -> Optional[datetime]:
        if not value:
            return None
        text = str(value).strip()
        try:
            if len(text) == 8 and text.isdigit():
                parsed = datetime.strptime(text, "%Y%m%d")
            elif len(text) == 10:
                parsed = datetime.strptime(text, "%Y-%m-%d")
            else:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone() if parsed.tzinfo else parsed.astimezone()

    @classmethod
    def _days_remaining(cls, record: Dict[str, Any]) -> Optional[int]:
        value = next(
            (record.get(key) for key in ("expiry_date", "expires_at", "expiryDate", "expiresAt", "expiration_date") if record.get(key)),
            None,
        )
        expiry = cls._parse_expiry(value)
        if not expiry:
            return None
        return (expiry.date() - datetime.now().astimezone().date()).days

    async def run_renewal(self, force: bool = False) -> Dict[str, Any]:
        async with self._renewal_lock:
            settings = self.store.renewal
            if not settings.enabled and not force:
                return {"status": "disabled", "checked": 0, "renewed": 0, "skipped": 0, "failed": 0}
            checked = renewed_count = skipped = failed = 0
            errors: List[Dict[str, str]] = []
            for token in self.store.tokens.values():
                if not token.enabled:
                    continue
                client = self.client_factory(token.token, self.api_base)
                try:
                    inventory = await _run_sync(client.list_domains)
                except DigitalPlatAPIError as error:
                    failed += 1
                    errors.append({"token": token.name, "error": str(error)})
                    continue
                for item in inventory:
                    domain = str(item.get("name") or item.get("domain") or "").strip().lower().rstrip(".")
                    if not HOSTNAME_PATTERN.fullmatch(domain):
                        continue
                    checked += 1
                    record = next((d for d in self.store.domains if str(d.get("domain", "")).lower() == domain), None)
                    if record is None:
                        record = {
                            "domain": domain,
                            "token_id": token.id,
                            "token_name": token.name,
                            "subscription_id": None,
                            "slot_type": item.get("slot_type") or item.get("lifecycle_type") or "unknown",
                            "nameservers": item.get("nameservers", []),
                            "registered_at": item.get("registration_date") or _timestamp(),
                            "status": item.get("status", "ok"),
                            "source": "renewal_sync",
                        }
                        self.store.domains.append(record)
                    record["renewal_checked_at"] = _timestamp()
                    if self._days_remaining(item) is None:
                        try:
                            item = await _run_sync(client.get_domain, domain)
                        except DigitalPlatAPIError as error:
                            record["renewal_status"] = "failed"
                            record["renewal_error"] = _safe_error(error)
                            failed += 1
                            errors.append({"domain": domain, "token": token.name, "error": str(error)})
                            continue
                    days_remaining = self._days_remaining(item)
                    record["expiry_date"] = next(
                        (item.get(key) for key in ("expiry_date", "expires_at", "expiryDate", "expiresAt", "expiration_date") if item.get(key)),
                        record.get("expiry_date"),
                    )
                    record["renewal_days_remaining"] = days_remaining
                    can_renew = next(
                        (item.get(key) for key in ("can_free_renew", "can_renew", "renewable") if key in item),
                        None,
                    )
                    if isinstance(can_renew, str):
                        can_renew = can_renew.strip().lower() in {"1", "true", "yes", "y"}
                    if can_renew is False:
                        record["renewal_status"] = "skipped"
                        skipped += 1
                        continue
                    if not force and (days_remaining is None or days_remaining > settings.renew_before_days):
                        record["renewal_status"] = "skipped"
                        skipped += 1
                        continue
                    previous_expiry = self._parse_expiry(record.get("expiry_date"))
                    try:
                        renewed_record = await _run_sync(
                            client.renew_domain,
                            domain,
                            settings.renewal_type,
                            settings.renewal_years,
                        )
                        record["renewal_status"] = "renewed"
                        record["renewal_error"] = None
                        record["expiry_date"] = next(
                            (renewed_record.get(key) for key in ("expiry_date", "expires_at", "expiryDate", "expiresAt", "expiration_date") if renewed_record.get(key)),
                            record.get("expiry_date"),
                        )
                        record["renewed_at"] = _timestamp()
                        renewed_count += 1
                    except DigitalPlatAPIError as error:
                        reconciled = False
                        if error.ambiguous:
                            try:
                                detail = await _run_sync(client.get_domain, domain)
                                current_expiry = self._parse_expiry(next(
                                    (detail.get(key) for key in ("expiry_date", "expires_at", "expiryDate", "expiresAt", "expiration_date") if detail.get(key)),
                                    None,
                                ))
                                reconciled = bool(current_expiry and previous_expiry and current_expiry > previous_expiry)
                                if reconciled:
                                    record["expiry_date"] = current_expiry.date().isoformat()
                            except DigitalPlatAPIError:
                                reconciled = False
                        if reconciled:
                            record["renewal_status"] = "renewed"
                            record["renewal_error"] = None
                            record["renewed_at"] = _timestamp()
                            renewed_count += 1
                        else:
                            record["renewal_status"] = "failed"
                            record["renewal_error"] = _safe_error(error)
                            failed += 1
                            errors.append({"domain": domain, "token": token.name, "error": str(error)})
                    delay = random.uniform(settings.delay_min_seconds, settings.delay_max_seconds)
                    if delay > 0:
                        await asyncio.sleep(delay)
            summary = {
                "status": "completed" if not errors else "completed_with_errors",
                "checked": checked,
                "renewed": renewed_count,
                "skipped": skipped,
                "failed": failed,
                "errors": errors[:50],
            }
            settings.last_run_at = _timestamp()
            settings.last_status = summary["status"]
            settings.last_error = errors[0]["error"] if errors else None
            settings.last_summary = summary
            await self.store.save()
            return summary

    async def start_renewal_scheduler(self) -> None:
        if self.renewal_task and not self.renewal_task.done():
            return
        self.renewal_task = asyncio.create_task(self._renewal_loop())

    async def stop_renewal_scheduler(self) -> None:
        if self.renewal_task and not self.renewal_task.done():
            self.renewal_task.cancel()
            try:
                await self.renewal_task
            except asyncio.CancelledError:
                pass

    async def _renewal_loop(self) -> None:
        while True:
            await asyncio.sleep(max(60, self.store.renewal.interval_seconds))
            try:
                await self.run_renewal()
            except Exception as error:
                self.store.renewal.last_status = "failed"
                self.store.renewal.last_error = _safe_error(error)
                await self.store.save()

    @staticmethod
    def _cloudflare_step(name: str, status: str, message: str) -> Dict[str, Any]:
        return {
            "name": name,
            "label": CLOUDFLARE_STEP_LABELS[name],
            "status": status,
            "message": message,
            "timestamp": _timestamp(),
        }

    def _domain_record(self, domain: str) -> Dict[str, Any]:
        normalized = str(domain or "").strip().lower().rstrip(".")
        for record in self.store.domains:
            if str(record.get("domain", "")).lower() == normalized:
                return record
        raise ValueError("Registered domain was not found in local automation history")

    async def host_domain_on_cloudflare(self, domain: str) -> Dict[str, Any]:
        record = self._domain_record(domain)
        token = self.store.tokens.get(str(record.get("token_id", "")))
        if not token:
            raise ValueError("The DigitalPlat API Token for this domain is missing")

        await self._wait_cloudflare_operation()
        record["cloudflare_status"] = "running"
        record["cloudflare_error"] = None
        record["cloudflare_steps"] = []
        await self.store.save()
        cloudflare = self._cloudflare_client()
        digitalplat = self.client_factory(token.token, self.api_base)

        try:
            record["cloudflare_steps"].append(
                self._cloudflare_step("zone_provisioning", "running", "Checking Cloudflare Zone")
            )
            await self.store.save()
            zone = await _run_sync(cloudflare.find_zone, record["domain"])
            if zone:
                record["cloudflare_steps"][-1] = self._cloudflare_step(
                    "zone_provisioning", "success", "Existing Zone found"
                )
            else:
                zone = await _run_sync(cloudflare.create_zone, record["domain"])
                record["cloudflare_steps"][-1] = self._cloudflare_step(
                    "zone_provisioning", "success", "Zone created"
                )

            zone_id = str(zone.get("id", ""))
            nameservers = [
                str(item).strip().lower().rstrip(".")
                for item in zone.get("name_servers", [])
                if str(item).strip()
            ]
            if not zone_id or len(nameservers) < 2 or any(
                not HOSTNAME_PATTERN.fullmatch(item) for item in nameservers
            ):
                raise CloudflareAPIError("Cloudflare did not return two valid nameservers")
            record["cloudflare_steps"].append(
                self._cloudflare_step(
                    "nameserver_assignment", "success", " · ".join(nameservers)
                )
            )
            record["cloudflare_zone_id"] = zone_id
            record["cloudflare_nameservers"] = nameservers
            await self.store.save()

            record["cloudflare_steps"].append(
                self._cloudflare_step(
                    "digitalplat_delegation", "running", "Updating DigitalPlat nameservers"
                )
            )
            await self.store.save()
            await _run_sync(digitalplat.update_nameservers, record["domain"], nameservers)
            record["cloudflare_steps"][-1] = self._cloudflare_step(
                "digitalplat_delegation", "success", "Nameserver delegation updated"
            )
            record["nameservers"] = nameservers

            latest_zone = await _run_sync(cloudflare.get_zone, zone_id)
            zone_status = str(latest_zone.get("status", zone.get("status", "pending"))).lower()
            active = zone_status == "active"
            record["cloudflare_steps"].append(
                self._cloudflare_step(
                    "cloudflare_activation",
                    "success" if active else "pending",
                    "Cloudflare is active" if active else "Waiting for DNS propagation; refresh later",
                )
            )
            record["cloudflare_status"] = "active" if active else "pending"
            record["cloudflare_checked_at"] = _timestamp()
        except (CloudflareAPIError, DigitalPlatAPIError, ValueError) as error:
            if record.get("cloudflare_steps") and record["cloudflare_steps"][-1].get("status") == "running":
                name = record["cloudflare_steps"][-1]["name"]
                record["cloudflare_steps"][-1] = self._cloudflare_step(name, "failed", str(error))
            record["cloudflare_status"] = "failed"
            record["cloudflare_error"] = _safe_error(error)
        await self.store.save()
        return record

    @staticmethod
    def normalize_subscription(request: Dict[str, Any]) -> Dict[str, Any]:
        prefix = str(request.get("prefix", "")).strip().lower()
        suffix = str(request.get("suffix", "")).strip().lower().lstrip(".")
        separator = str(request.get("separator", DEFAULT_SEPARATOR)).strip()
        random_length = request.get("random_length", 6)
        slot_type = str(request.get("slot_type", "subscription")).strip().lower()
        auto_cloudflare = bool(request.get("auto_cloudflare", False))
        nameservers = [str(value).strip().lower().rstrip(".") for value in request.get("nameservers", []) if str(value).strip()]
        if prefix and not LABEL_PATTERN.fullmatch(prefix):
            raise ValueError("Prefix must contain only lowercase letters, numbers, and hyphens")
        if not HOSTNAME_PATTERN.fullmatch(suffix):
            raise ValueError("Suffix must be a valid domain suffix")
        if separator not in {"", "-"}:
            raise ValueError("Separator must be empty or a hyphen")
        if not isinstance(random_length, int) or not 2 <= random_length <= 24:
            raise ValueError("Random length must be between 2 and 24")
        if slot_type not in {"free", "paid", "subscription"}:
            raise ValueError("Slot type must be free, paid, or subscription")
        # DigitalPlat's dpdns.org registration flow rejects the generated
        # dashed labels used by the old UI. Keep the stored option readable,
        # but always generate a compact label for this suffix.
        if suffix == "dpdns.org":
            separator = DEFAULT_SEPARATOR
        if (not auto_cloudflare and len(nameservers) < 2) or any(
            not HOSTNAME_PATTERN.fullmatch(value) for value in nameservers
        ):
            raise ValueError(
                "At least two valid nameservers are required unless Cloudflare automation is enabled"
            )
        if len(prefix) + len(separator) + random_length > 63:
            raise ValueError("Generated domain label would exceed 63 characters")
        return {
            "name": str(request.get("name") or f"{prefix or 'random'}.{suffix}").strip()[:80],
            "prefix": prefix,
            "suffix": suffix,
            "nameservers": nameservers,
            "slot_type": slot_type,
            "random_length": random_length,
            "separator": separator,
            "auto_cloudflare": auto_cloudflare,
            "enabled": bool(request.get("enabled", True)),
        }

    async def test_token(self, token_id: str) -> Dict[str, Any]:
        record = self.store.tokens.get(token_id)
        if not record:
            raise KeyError(token_id)
        try:
            domains = await _run_sync(
                self.client_factory(record.token, self.api_base).list_domains
            )
            record.last_status = "valid"
            record.last_error = None
            record.domain_count = len(domains)
        except DigitalPlatAPIError as error:
            record.last_status = "invalid"
            record.last_error = _safe_error(error)
            record.domain_count = None
        record.last_checked_at = _timestamp()
        await self.store.save()
        return record.safe_dict()

    async def sync_domains(self) -> Dict[str, Any]:
        synced = 0
        errors: List[Dict[str, str]] = []
        by_domain = {
            str(item.get("domain", "")).lower(): item
            for item in self.store.domains
            if item.get("domain")
        }
        for token in self.store.tokens.values():
            if not token.enabled:
                continue
            try:
                inventory = await _run_sync(
                    self.client_factory(token.token, self.api_base).list_domains
                )
            except DigitalPlatAPIError as error:
                errors.append({"token_id": token.id, "error": str(error)})
                continue
            for item in inventory:
                domain = str(item.get("name") or item.get("domain") or "").lower().rstrip(".")
                if not HOSTNAME_PATTERN.fullmatch(domain):
                    continue
                nameservers = [
                    str(value).lower().rstrip(".")
                    for value in item.get("nameservers", [])
                    if str(value).strip()
                ]
                record = by_domain.get(domain)
                if not record:
                    record = {
                        "domain": domain,
                        "token_id": token.id,
                        "token_name": token.name,
                        "subscription_id": None,
                        "slot_type": item.get("slot_type") or item.get("lifecycle_type") or "unknown",
                        "nameservers": nameservers,
                        "registered_at": item.get("registration_date") or _timestamp(),
                        "status": item.get("status", "ok"),
                        "expiry_date": next(
                            (item.get(key) for key in ("expiry_date", "expires_at", "expiryDate", "expiresAt", "expiration_date") if item.get(key)),
                            None,
                        ),
                        "source": "digitalplat_sync",
                    }
                    self.store.domains.append(record)
                    by_domain[domain] = record
                    synced += 1
                else:
                    record["token_id"] = token.id
                    record["token_name"] = token.name
                    if nameservers:
                        record["nameservers"] = nameservers
                    record["status"] = item.get("status", record.get("status", "ok"))
                    record["expiry_date"] = next(
                        (item.get(key) for key in ("expiry_date", "expires_at", "expiryDate", "expiresAt", "expiration_date") if item.get(key)),
                        record.get("expiry_date"),
                    )
        await self.store.save()
        return {
            "synced": synced,
            "total": len(self.store.domains),
            "errors": errors,
        }

    def _candidate(self, subscription: PrefixSubscription) -> str:
        alphabet = string.ascii_lowercase + string.digits
        random_part = "".join(random.SystemRandom().choice(alphabet) for _ in range(subscription.random_length))
        separator = "" if subscription.suffix == "dpdns.org" else subscription.separator
        label = f"{subscription.prefix}{separator if subscription.prefix else ''}{random_part}"
        return f"{label}.{subscription.suffix}"

    @staticmethod
    def _step(name: str, status: str, message: str) -> Dict[str, Any]:
        return {
            "name": name,
            "label": DOMAIN_STEP_LABELS[name],
            "status": status,
            "message": message,
            "timestamp": _timestamp(),
        }

    async def start_job(
        self,
        subscription_id: str,
        target_count: int,
        token_ids: Optional[List[str]] = None,
        max_attempts: Optional[int] = None,
        delay_min_seconds: float = 20.0,
        delay_max_seconds: float = 45.0,
    ) -> DomainRegistrationJob:
        subscription = self.store.subscriptions.get(subscription_id)
        if not subscription or not subscription.enabled:
            raise ValueError("Prefix subscription is missing or disabled")
        selected = token_ids or [item.id for item in self.store.tokens.values() if item.enabled]
        selected = [token_id for token_id in selected if self.store.tokens.get(token_id) and self.store.tokens[token_id].enabled]
        if not selected:
            raise ValueError("At least one enabled API Token is required")
        if subscription.auto_cloudflare and not self.store.cloudflare:
            raise ValueError("Cloudflare configuration is required for this subscription")
        if not isinstance(target_count, int) or not 1 <= target_count <= 100:
            raise ValueError("Target count must be between 1 and 100")
        attempt_limit = max_attempts or target_count * 10
        attempt_limit = max(target_count, min(int(attempt_limit), 1000))
        try:
            delay_min_seconds = float(delay_min_seconds)
            delay_max_seconds = float(delay_max_seconds)
        except (TypeError, ValueError) as error:
            raise ValueError("Domain delay must be a number") from error
        if delay_min_seconds < 0 or delay_max_seconds < delay_min_seconds or delay_max_seconds > 3600:
            raise ValueError("Domain delay must satisfy 0 <= minimum <= maximum <= 3600")
        job = DomainRegistrationJob(
            subscription_id=subscription_id,
            target_count=target_count,
            token_ids=selected,
            max_attempts=attempt_limit,
            delay_min_seconds=delay_min_seconds,
            delay_max_seconds=delay_max_seconds,
        )
        self.store.jobs[job.id] = job
        await self.store.save()
        self.tasks[job.id] = asyncio.create_task(self._run_job(job.id))
        return job

    async def _run_job(self, job_id: str) -> None:
        job = self.store.jobs[job_id]
        subscription = self.store.subscriptions[job.subscription_id]
        job.status = "running"
        job.started_at = _timestamp()
        unavailable_tokens: set = set()
        existing_candidates = {attempt.domain for attempt in job.attempts}
        token_cursor = 0
        await self.store.save()

        try:
            while (
                job.successful_domains < job.target_count
                and job.completed_attempts < job.max_attempts
            ):
                usable_ids = [token_id for token_id in job.token_ids if token_id not in unavailable_tokens]
                if not usable_ids:
                    job.error = "All selected API Tokens are unavailable or have no usable capacity"
                    break
                token_id = usable_ids[token_cursor % len(usable_ids)]
                token_cursor += 1
                token = self.store.tokens[token_id]
                domain = self._candidate(subscription)
                while domain in existing_candidates:
                    domain = self._candidate(subscription)
                existing_candidates.add(domain)
                attempt = DomainAttempt(domain=domain, token_id=token.id, token_name=token.name)
                attempt.steps.append(self._step("candidate_generation", "success", f"Generated {domain}"))
                attempt.steps.append(self._step("token_assignment", "success", f"Assigned token {token.name}"))
                job.attempts.append(attempt)
                await self.store.save()

                client = self.client_factory(token.token, self.api_base)
                try:
                    attempt.steps.append(self._step("registration_request", "running", "Sending registration request"))
                    await self.store.save()
                    response = await _run_sync(
                        client.register_domain,
                        domain,
                        subscription.slot_type,
                        [] if subscription.auto_cloudflare else subscription.nameservers,
                    )
                    attempt.steps[-1] = self._step("registration_request", "success", "Registration accepted")
                    returned_name = str(response.get("name") or response.get("domain") or domain).lower()
                    if returned_name != domain:
                        raise DigitalPlatAPIError("Registration response returned a different domain")
                    attempt.steps.append(self._step("registration_verification", "success", "Confirmed by API response"))
                    attempt.status = "succeeded"
                    attempt.response = {
                        "name": returned_name,
                        "status": response.get("status"),
                        "slot_type": response.get("slot_type"),
                        "lifecycle_type": response.get("lifecycle_type"),
                    }
                    job.successful_domains += 1
                    domain_record = {
                        "domain": returned_name,
                        "token_id": token.id,
                        "token_name": token.name,
                        "subscription_id": subscription.id,
                        "slot_type": subscription.slot_type,
                        "nameservers": [] if subscription.auto_cloudflare else subscription.nameservers,
                        "registered_at": _timestamp(),
                        "status": response.get("status", "ok"),
                    }
                    self.store.domains.append(domain_record)
                    await self.store.save()
                    if subscription.auto_cloudflare and self.store.cloudflare:
                        await self.host_domain_on_cloudflare(returned_name)
                except DigitalPlatAPIError as error:
                    if attempt.steps[-1].get("name") == "registration_request" and attempt.steps[-1].get("status") == "running":
                        attempt.steps[-1] = self._step("registration_request", "failed", str(error))
                    reconciled = False
                    if error.ambiguous:
                        attempt.steps.append(self._step("registration_verification", "running", "Request result unclear; checking domain inventory"))
                        await self.store.save()
                        try:
                            domains = await _run_sync(client.list_domains)
                            reconciled = any(
                                str(item.get("name") or item.get("domain", "")).lower() == domain
                                for item in domains
                            )
                        except DigitalPlatAPIError:
                            reconciled = False
                    if reconciled:
                        attempt.steps[-1] = self._step("registration_verification", "success", "Domain found during reconciliation")
                        attempt.status = "succeeded"
                        job.successful_domains += 1
                        domain_record = {
                            "domain": domain,
                            "token_id": token.id,
                            "token_name": token.name,
                            "subscription_id": subscription.id,
                            "slot_type": subscription.slot_type,
                            "nameservers": [] if subscription.auto_cloudflare else subscription.nameservers,
                            "registered_at": _timestamp(),
                            "status": "ok",
                        }
                        self.store.domains.append(domain_record)
                        await self.store.save()
                        if subscription.auto_cloudflare and self.store.cloudflare:
                            await self.host_domain_on_cloudflare(domain)
                    else:
                        if error.ambiguous:
                            attempt.steps[-1] = self._step("registration_verification", "failed", "Domain not confirmed; candidate will not be retried")
                        elif not any(step["name"] == "registration_verification" for step in attempt.steps):
                            attempt.steps.append(self._step("registration_verification", "skipped", "Registration request failed"))
                        attempt.status = "failed"
                        attempt.error = _safe_error(error)
                        job.failed_attempts += 1
                        message = str(error).lower()
                        if error.status_code in {401, 403, 429} or any(
                            marker in message for marker in ("slot", "capacity", "subscription", "quota", "limit")
                        ):
                            unavailable_tokens.add(token.id)
                finally:
                    attempt.finished_at = _timestamp()
                    job.completed_attempts += 1
                    await self.store.save()

                if (
                    job.successful_domains < job.target_count
                    and job.completed_attempts < job.max_attempts
                ):
                    delay = random.uniform(job.delay_min_seconds, job.delay_max_seconds)
                    if delay > 0:
                        await asyncio.sleep(delay)

            job.status = "completed" if job.successful_domains >= job.target_count else "failed"
            if job.status == "failed" and not job.error:
                job.error = f"Stopped after {job.completed_attempts} attempts with {job.successful_domains} successful registrations"
        except asyncio.CancelledError:
            job.status = "paused"
            job.error = "Task cancelled"
        except Exception as error:
            job.status = "failed"
            job.error = _safe_error(error)
        finally:
            job.finished_at = _timestamp()
            await self.store.save()

    def overview(self) -> Dict[str, Any]:
        jobs = sorted(self.store.jobs.values(), key=lambda item: item.created_at, reverse=True)
        return {
            "tokens": [item.safe_dict() for item in self.store.tokens.values()],
            "subscriptions": [item.to_dict() for item in self.store.subscriptions.values()],
            "jobs": [item.to_dict() for item in jobs[:50]],
            "domains": list(reversed(self.store.domains[-200:])),
            "cloudflare": self.store.cloudflare.safe_dict() if self.store.cloudflare else None,
            "renewal": self.store.renewal.safe_dict(),
            "stats": {
                "tokens": len(self.store.tokens),
                "enabled_tokens": sum(item.enabled for item in self.store.tokens.values()),
                "subscriptions": len(self.store.subscriptions),
                "running_jobs": sum(item.status == "running" for item in jobs),
                "registered_domains": len(self.store.domains),
                "cloudflare_active": sum(
                    item.get("cloudflare_status") == "active" for item in self.store.domains
                ),
            },
        }
