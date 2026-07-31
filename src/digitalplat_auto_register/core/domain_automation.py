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
DEFAULT_DATA_PATH = "/app/data/domain-automation.json"
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
    separator: str = "-"
    enabled: bool = True
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    created_at: str = field(default_factory=_timestamp)

    def to_dict(self) -> Dict[str, Any]:
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
    ) -> None:
        self.store = store
        self.client_factory = client_factory
        self.tasks: Dict[str, asyncio.Task] = {}
        self.api_base = os.getenv("DIGITALPLAT_API_BASE", DEFAULT_API_BASE)

    @staticmethod
    def validate_token(token: str) -> str:
        value = str(token or "").strip()
        if not TOKEN_PATTERN.fullmatch(value):
            raise ValueError("Token must start with dp_live_ or dp_test_")
        return value

    @staticmethod
    def normalize_subscription(request: Dict[str, Any]) -> Dict[str, Any]:
        prefix = str(request.get("prefix", "")).strip().lower()
        suffix = str(request.get("suffix", "")).strip().lower().lstrip(".")
        separator = str(request.get("separator", "-")).strip()
        random_length = request.get("random_length", 6)
        slot_type = str(request.get("slot_type", "subscription")).strip().lower()
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
        if len(nameservers) < 2 or any(not HOSTNAME_PATTERN.fullmatch(value) for value in nameservers):
            raise ValueError("At least two valid nameserver hostnames are required")
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

    def _candidate(self, subscription: PrefixSubscription) -> str:
        alphabet = string.ascii_lowercase + string.digits
        random_part = "".join(random.SystemRandom().choice(alphabet) for _ in range(subscription.random_length))
        label = f"{subscription.prefix}{subscription.separator if subscription.prefix else ''}{random_part}"
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
    ) -> DomainRegistrationJob:
        subscription = self.store.subscriptions.get(subscription_id)
        if not subscription or not subscription.enabled:
            raise ValueError("Prefix subscription is missing or disabled")
        selected = token_ids or [item.id for item in self.store.tokens.values() if item.enabled]
        selected = [token_id for token_id in selected if self.store.tokens.get(token_id) and self.store.tokens[token_id].enabled]
        if not selected:
            raise ValueError("At least one enabled API Token is required")
        if not isinstance(target_count, int) or not 1 <= target_count <= 100:
            raise ValueError("Target count must be between 1 and 100")
        attempt_limit = max_attempts or target_count * 10
        attempt_limit = max(target_count, min(int(attempt_limit), 1000))
        job = DomainRegistrationJob(
            subscription_id=subscription_id,
            target_count=target_count,
            token_ids=selected,
            max_attempts=attempt_limit,
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
                        subscription.nameservers,
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
                    self.store.domains.append({
                        "domain": returned_name,
                        "token_id": token.id,
                        "token_name": token.name,
                        "subscription_id": subscription.id,
                        "slot_type": subscription.slot_type,
                        "nameservers": subscription.nameservers,
                        "registered_at": _timestamp(),
                        "status": response.get("status", "ok"),
                    })
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
                        self.store.domains.append({
                            "domain": domain,
                            "token_id": token.id,
                            "token_name": token.name,
                            "subscription_id": subscription.id,
                            "slot_type": subscription.slot_type,
                            "nameservers": subscription.nameservers,
                            "registered_at": _timestamp(),
                            "status": "ok",
                        })
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
            "stats": {
                "tokens": len(self.store.tokens),
                "enabled_tokens": sum(item.enabled for item in self.store.tokens.values()),
                "subscriptions": len(self.store.subscriptions),
                "running_jobs": sum(item.status == "running" for item in jobs),
                "registered_domains": len(self.store.domains),
            },
        }
