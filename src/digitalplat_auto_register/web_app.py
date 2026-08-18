"""Web management console for batch DigitalPlat registrations and account management."""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import random
import secrets
import tempfile
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Import new feature modules
from .core.account_pool import AccountPool, SelectionStrategy, AccountStatus as PoolAccountStatus
from .core.statistics import StatisticsCollector, MetricType
from .utils.enhanced_logging import setup_enhanced_logging, get_logger, log_aggregator, perf_tracker
from .web_routes import create_api_router

logger = logging.getLogger(__name__)

from .core.account import (
    Account, AccountStatus, AccountStore, BatchRegistrationJob
)
from .core.domain_automation import (
    APITokenRecord,
    CloudflareSettings,
    DomainAutomationManager,
    DomainAutomationStore,
    PrefixSubscription,
)
from .core.registrar import register_with_defaults
from .core.result import StepResult

DEFAULT_REFERRAL_CODE = "4qn8iw8r1o"
DEFAULT_JOBS_PATH = "/app/data/jobs.json"
DEFAULT_ACCOUNTS_PATH = "/app/data/accounts.json"

_PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(_PACKAGE_DIR / "templates"))


def asset_version() -> str:
    """Digest of static file mtimes; appended to asset URLs to defeat browser caching."""
    digest = hashlib.md5()
    try:
        for path in sorted((_PACKAGE_DIR / "static").rglob("*")):
            if path.is_file():
                info = path.stat()
                digest.update(f"{path.name}:{info.st_mtime_ns}:{info.st_size}".encode())
    except OSError:
        return "0"
    return digest.hexdigest()[:10]


TEMPLATES.env.globals["asset_version"] = asset_version

MAX_JOB_HISTORY = 50
MAX_CONCURRENT_REGISTRATIONS = 3
RUNNING_INTERRUPTED_MESSAGE = "Registration stopped because the service restarted."
REGISTRATION_STEP_ORDER = (
    "turnstile_token_acquisition",
    "email_creation",
    "browser_navigation",
    "form_submission",
    "verification_email_retrieval",
    "verification_completion",
)
REGISTRATION_STEP_LABELS = {
    "turnstile_token_acquisition": "Turnstile 验证",
    "email_creation": "创建临时邮箱",
    "browser_navigation": "打开注册页面",
    "form_submission": "提交注册表单",
    "verification_email_retrieval": "获取验证邮件",
    "verification_completion": "完成邮箱验证",
}


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _generate_phone_number() -> str:
    """Return the API-required +<calling-code>-<digits> format."""
    subscriber_number = 2_000_000_000 + secrets.randbelow(8_000_000_000)
    return f"+1-{subscriber_number:010d}"


def _generate_username() -> str:
    """Generate a random username."""
    import random
    import string
    import time
    timestamp = str(int(time.time()))[-6:]
    random_part = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"user_{timestamp}_{random_part}"


def _generate_password() -> str:
    """Generate a random password."""
    import random
    import string
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(random.choices(chars, k=16))


def _safe_text(value: Any, limit: int = 1000) -> Optional[str]:
    """Sanitize text for display."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


class OperationPacer:
    """Serialize sensitive external operations and add a random cooldown."""

    def __init__(self, minimum: float = 15.0, maximum: float = 30.0) -> None:
        self.minimum = max(0.0, float(minimum))
        self.maximum = max(self.minimum, float(maximum))
        self._lock = asyncio.Lock()
        self._last_started = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._last_started:
                cooldown = random.uniform(self.minimum, self.maximum)
                remaining = cooldown - (now - self._last_started)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            self._last_started = time.monotonic()


# Legacy job tracking for backward compatibility
@dataclass
class RegistrationJob:
    """The deliberately limited, safe-to-persist state for one request."""

    id: str
    referral_code: str = DEFAULT_REFERRAL_CODE
    status: str = "running"
    created_at: str = field(default_factory=_timestamp)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    steps: List[Dict[str, Any]] = field(default_factory=list)
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    task: Optional[asyncio.Task] = field(default=None, repr=False)
    account_id: Optional[str] = None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "referral_code": self.referral_code,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "steps": self.steps,
            "result": self.result,
            "error": _safe_text(self.error),
            "account_id": self.account_id,
        }


class RegistrationManager:
    """Manage batch registration jobs and coordinate with AccountStore."""

    def __init__(
        self,
        account_store: AccountStore,
        jobs_path: Optional[Path] = None,
    ) -> None:
        self._account_store = account_store
        self._jobs_path = Path(jobs_path or os.getenv("JOBS_PATH", DEFAULT_JOBS_PATH))
        self._jobs: "OrderedDict[str, RegistrationJob]" = OrderedDict()
        self._active_job_ids: set = set()
        self._lock = asyncio.Lock()
        self._loaded = False
        self._batch_task: Optional[asyncio.Task] = None

    async def load(self) -> None:
        """Load prior jobs and close any job interrupted by a process restart."""
        async with self._lock:
            if self._loaded:
                return
            self._loaded = True
            if self._jobs_path.exists():
                try:
                    payload = json.loads(self._jobs_path.read_text(encoding="utf-8"))
                    raw_jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
                except (OSError, ValueError):
                    raw_jobs = []
                for raw in raw_jobs:
                    job = self._job_from_snapshot(raw)
                    if job is None:
                        continue
                    if job.status == "running":
                        job.status = "failed"
                        job.finished_at = _timestamp()
                        job.error = RUNNING_INTERRUPTED_MESSAGE
                    self._jobs[job.id] = job
            self._trim_history()

    async def start_single(self) -> RegistrationJob:
        """Start a single account registration."""
        async with self._lock:
            if self._active_job_ids:
                raise RuntimeError("A single registration is already running")
            job = RegistrationJob(id=uuid4().hex[:12])
            self._jobs[job.id] = job
            self._active_job_ids.add(job.id)
            self._trim_history()
            job.task = asyncio.create_task(self._run_single(job))
            return job

    async def start_batch(
        self,
        count: int,
        referral_code: str = DEFAULT_REFERRAL_CODE,
        username_prefix: Optional[str] = None,
        delay: float = 5.0,
        delay_max: Optional[float] = None,
        max_concurrent: int = 1,
        turnstile_sitekey: Optional[str] = None,
        turnstile_endpoint: Optional[str] = None,
    ) -> BatchRegistrationJob:
        """Start a batch registration job creating multiple accounts."""
        try:
            delay = float(delay)
            delay_max = float(delay if delay_max is None else delay_max)
            max_concurrent = int(max_concurrent)
        except (TypeError, ValueError) as error:
            raise ValueError("Registration delay and concurrency must be numeric") from error
        if delay < 0 or delay_max < delay or delay_max > 3600:
            raise ValueError("Registration delay must satisfy 0 <= minimum <= maximum <= 3600")
        if not 1 <= max_concurrent <= MAX_CONCURRENT_REGISTRATIONS:
            raise ValueError(
                f"Registration concurrency must be between 1 and {MAX_CONCURRENT_REGISTRATIONS}"
            )
        async with self._lock:
            # Create account entries for each registration
            account_ids = []
            for i in range(count):
                username = f"{username_prefix or 'user'}_{uuid4().hex[:8]}"
                account = Account(
                    username=username,
                    email="",  # Will be auto-generated
                    password=_generate_password(),
                    referral_code=referral_code,
                    status=AccountStatus.PENDING,
                )
                self._account_store.create_account(account)
                account_ids.append(account.id)

            # Create batch job
            batch_job = BatchRegistrationJob(
                referral_code=referral_code,
                status="pending",
                total_accounts=len(account_ids),
                account_ids=account_ids,
                delay_between_registrations=delay,
                delay_min_seconds=delay,
                delay_max_seconds=delay_max,
                max_concurrent=max_concurrent,
                username_prefix=username_prefix,
            )
            self._account_store.create_batch_job(batch_job)

            # Store turnstile config in batch job for use during registration
            if turnstile_sitekey or turnstile_endpoint:
                batch_job.metadata['turnstile_sitekey'] = turnstile_sitekey
                batch_job.metadata['turnstile_endpoint'] = turnstile_endpoint

            # Start batch processing
            self._batch_task = asyncio.create_task(self._run_batch(batch_job.id))
            
            return batch_job

    async def _run_single(self, job: RegistrationJob) -> None:
        """Execute single registration with auto-generated credentials."""
        job.started_at = _timestamp()
        
        # Create account record
        account = Account(
            username=_generate_username(),
            email="",
            password=_generate_password(),
            referral_code=job.referral_code,
            status=AccountStatus.REGISTERING,
        )
        self._account_store.create_account(account)
        job.account_id = account.id
        account.metadata.setdefault("steps", [])
        account.metadata["current_step"] = REGISTRATION_STEP_ORDER[0]

        def on_step_complete(step: StepResult) -> None:
            step_data = {
                "name": step.name,
                "success": step.success,
                "duration": step.duration,
                "message": step.message,
                "error": step.error,
                "timestamp": step.timestamp.isoformat(),
            }
            job.steps.append(step_data)
            account.metadata["steps"].append(step_data)
            try:
                current_index = REGISTRATION_STEP_ORDER.index(step.name)
            except ValueError:
                current_index = -1
            account.metadata["current_step"] = (
                REGISTRATION_STEP_ORDER[current_index + 1]
                if current_index + 1 < len(REGISTRATION_STEP_ORDER)
                else None
            )
            asyncio.create_task(self._account_store.save())

        try:
            result = await register_with_defaults(
                username=account.username,
                password=account.password,
                referral_code=job.referral_code,
                phone=_generate_phone_number(),
                on_step_complete=on_step_complete,
            )
            
            # Update account with results
            if result.success:
                account.username = result.username or account.username
                account.email = result.email or ""
                if result.password:
                    account.password = result.password
                account.mark_active()
                job.result = {
                    "success": True,
                    "username": account.username,
                    "email": account.email,
                    "password": account.password,
                    "duration": result.total_duration,
                }
                job.status = "succeeded"
            else:
                failed_step = account.metadata.get("current_step")
                account.mark_failed(result.error or "Unknown error", failed_step or result.error_stage)
                if failed_step and not any(
                    step.get("name") == failed_step and step.get("success") is False
                    for step in account.metadata["steps"]
                ):
                    account.metadata["steps"].append({
                        "name": failed_step,
                        "success": False,
                        "duration": None,
                        "message": result.error or "注册步骤失败",
                        "error": result.error,
                        "timestamp": _timestamp(),
                    })
                job.error = result.error
                job.status = "failed"
                
        except Exception as e:
            account.mark_failed(str(e))
            job.error = _safe_text(str(e))
            job.status = "failed"
        finally:
            job.finished_at = _timestamp()
            self._active_job_ids.discard(job.id)
            account.metadata["current_step"] = None
            await self._account_store.save()

    async def _run_batch(self, batch_job_id: str) -> None:
        """Execute batch registration for multiple accounts."""
        batch_job = self._account_store.get_batch_job(batch_job_id)
        if not batch_job:
            return

        batch_job.status = "running"
        batch_job.started_at = _timestamp()
        await self._account_store.save()

        semaphore = asyncio.Semaphore(batch_job.max_concurrent)
        start_lock = asyncio.Lock()
        last_started = 0.0

        async def wait_for_start_slot() -> None:
            """Space registration starts while still allowing in-flight overlap."""
            nonlocal last_started
            async with start_lock:
                if last_started:
                    delay = random.uniform(
                        batch_job.delay_min_seconds,
                        batch_job.delay_max_seconds,
                    )
                    remaining = delay - (time.monotonic() - last_started)
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                last_started = time.monotonic()

        async def register_one(account_id: str) -> None:
            async with semaphore:
                await wait_for_start_slot()
                account = self._account_store.get_account(account_id)
                if not account or account.status != AccountStatus.PENDING:
                    return

                account.mark_registering()
                account.metadata.setdefault("steps", [])
                account.metadata["current_step"] = REGISTRATION_STEP_ORDER[0]
                await self._account_store.save()

                def on_step_complete(step: StepResult) -> None:
                    """Store step progress in account metadata for real-time display."""
                    if 'steps' not in account.metadata:
                        account.metadata['steps'] = []
                    step_dict = {
                        'name': step.name,
                        'success': step.success,
                        'duration': step.duration,
                        'message': step.message,
                        'timestamp': datetime.now().astimezone().isoformat()
                    }
                    if step.error:
                        step_dict['error'] = step.error
                    account.metadata['steps'].append(step_dict)
                    # Keep the UI focused on the next operation.  The callback is
                    # invoked after a step completes, so exposing the completed
                    # step as "current" made the progress indicator appear stuck.
                    try:
                        current_index = REGISTRATION_STEP_ORDER.index(step.name)
                    except ValueError:
                        current_index = -1
                    account.metadata['current_step'] = (
                        REGISTRATION_STEP_ORDER[current_index + 1]
                        if current_index + 1 < len(REGISTRATION_STEP_ORDER)
                        else None
                    )
                    asyncio.create_task(self._account_store.save())

                try:
                    # Get turnstile config from batch job metadata
                    ts_sitekey = batch_job.metadata.get('turnstile_sitekey')
                    ts_endpoint = batch_job.metadata.get('turnstile_endpoint')
                    
                    result = await register_with_defaults(
                        username=account.username,
                        password=account.password,
                        referral_code=batch_job.referral_code or DEFAULT_REFERRAL_CODE,
                        phone=_generate_phone_number(),
                        on_step_complete=on_step_complete,
                        turnstile_sitekey=ts_sitekey,
                        turnstile_endpoint=ts_endpoint,
                    )
                    
                    if result.success:
                        # Update account with actual registered values
                        account.email = result.email or ""
                        if result.username:
                            account.username = result.username
                        if result.password:
                            account.password = result.password
                        account.mark_active()
                        batch_job.successful_accounts += 1
                        logger.info(f"Account registered: {account.username} ({account.email})")
                    else:
                        failed_step = account.metadata.get("current_step")
                        account.mark_failed(result.error or "Unknown error", failed_step or result.error_stage)
                        batch_job.failed_accounts += 1
                        if failed_step and not any(
                            step.get("name") == failed_step and step.get("success") is False
                            for step in account.metadata["steps"]
                        ):
                            account.metadata["steps"].append({
                                "name": failed_step,
                                "success": False,
                                "duration": None,
                                "message": result.error or "注册步骤失败",
                                "error": result.error,
                                "timestamp": _timestamp(),
                            })
                        
                except Exception as e:
                    account.mark_failed(str(e))
                    batch_job.failed_accounts += 1
                    current_step = account.metadata.get("current_step") or "registration_workflow"
                    if not any(step.get("name") == current_step and not step.get("success") for step in account.metadata["steps"]):
                        account.metadata["steps"].append({
                            "name": current_step,
                            "success": False,
                            "duration": None,
                            "message": str(e),
                            "error": str(e),
                            "timestamp": _timestamp(),
                        })
                finally:
                    batch_job.completed_accounts += 1
                    account.metadata["current_step"] = None
                await self._account_store.save()

        try:
            tasks = [
                asyncio.create_task(register_one(account_id))
                for account_id in batch_job.account_ids
            ]
            await asyncio.gather(*tasks)
            
            batch_job.status = "completed"
            batch_job.error = None
            
        except asyncio.CancelledError:
            batch_job.status = "paused"
        except Exception as e:
            batch_job.status = "failed"
            batch_job.error = str(e)
        finally:
            batch_job.finished_at = _timestamp()
            await self._account_store.save()

    def get(self, job_id: str) -> Optional[RegistrationJob]:
        return self._jobs.get(job_id)

    def overview(self) -> Dict[str, Any]:
        jobs = [job.snapshot() for job in reversed(self._jobs.values())]
        return {
            "active_jobs": list(self._active_job_ids),
            "jobs": jobs,
            "total_jobs": len(jobs),
            "successful_jobs": sum(job["status"] == "succeeded" for job in jobs),
            "account_overview": self._account_store.get_overview(),
        }

    @staticmethod
    def account_progress(account: Account) -> Dict[str, Any]:
        """Return a stable, UI-ready view of every registration step."""
        recorded = {step.get("name"): step for step in account.metadata.get("steps", [])}
        is_incomplete = account.status in (AccountStatus.PENDING, AccountStatus.REGISTERING)
        steps = []
        for name in REGISTRATION_STEP_ORDER:
            item = recorded.get(name)
            if item:
                steps.append({**item, "label": REGISTRATION_STEP_LABELS[name], "status": "success" if item.get("success") else "failed"})
            else:
                steps.append({
                    "name": name,
                    "label": REGISTRATION_STEP_LABELS[name],
                    "status": "pending" if is_incomplete else "skipped",
                    "success": None,
                    "duration": None,
                    "message": "等待执行" if is_incomplete else "未执行",
                })
        current = account.metadata.get("current_step")
        if account.status == AccountStatus.REGISTERING and not current:
            current = next((step["name"] for step in steps if step["status"] == "pending"), None)
        return {
            "account_id": account.id,
            "username": account.username,
            "status": account.status.value,
            "current_step": current,
            "steps": steps,
            "total_steps": len(REGISTRATION_STEP_ORDER),
            "completed_steps": sum(step["status"] in ("success", "failed") for step in steps),
            "error": _safe_text(account.error),
            "error_stage": account.error_stage,
        }

    def _job_from_snapshot(self, raw: Any) -> Optional[RegistrationJob]:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("id"), str):
            return None
        job_data = {
            "id": raw["id"][:128],
            "referral_code": raw.get("referral_code", DEFAULT_REFERRAL_CODE),
            "status": raw.get("status", "failed"),
            "created_at": raw.get("created_at", _timestamp()),
            "started_at": raw.get("started_at"),
            "finished_at": raw.get("finished_at"),
            "steps": raw.get("steps", []),
            "result": raw.get("result"),
            "error": raw.get("error"),
            "account_id": raw.get("account_id"),
        }
        job = RegistrationJob(**job_data)
        if job.status not in {"running", "succeeded", "failed"}:
            job.status = "failed"
        return job

    def _trim_history(self) -> None:
        while len(self._jobs) > MAX_JOB_HISTORY:
            oldest_id, oldest = next(iter(self._jobs.items()))
            if oldest.id in self._active_job_ids:
                return
            self._jobs.pop(oldest_id)


def create_app(
    manager: Optional[RegistrationManager] = None,
    account_store: Optional[AccountStore] = None,
    domain_store: Optional[DomainAutomationStore] = None,
    domain_manager: Optional[DomainAutomationManager] = None,
    # New feature components (account pool, statistics, enhanced logging)
    pool_db_path: Optional[str] = None,
    stats_db_path: Optional[str] = None,
    enable_v2_api: bool = True,
) -> FastAPI:
    """Create FastAPI application with all routes."""

    fallback_data_dir: Optional[Path] = None

    def database_path(explicit: Optional[str], env_name: str, filename: str) -> str:
        nonlocal fallback_data_dir
        if explicit:
            return explicit
        configured = os.getenv(env_name)
        if configured:
            return configured
        production_dir = Path("/app/data")
        if production_dir.is_dir() and os.access(production_dir, os.W_OK):
            return str(production_dir / filename)
        if fallback_data_dir is None:
            fallback_data_dir = Path(tempfile.mkdtemp(prefix="digitalplat-web-"))
        return str(fallback_data_dir / filename)

    # Initialize new feature components
    pool = AccountPool(
        db_path=database_path(pool_db_path, "ACCOUNT_POOL_PATH", "account_pool.db")
    )
    stats = StatisticsCollector(
        db_path=database_path(stats_db_path, "STATISTICS_PATH", "statistics.db")
    )
    
    if account_store is None:
        account_store = AccountStore()
    # Wire up pool and stats for automatic sync
    account_store.set_pool(pool)
    account_store.set_stats(stats)
    if manager is None:
        manager = RegistrationManager(account_store)
    if domain_store is None:
        domain_store = DomainAutomationStore()
    if domain_manager is None:
        domain_manager = DomainAutomationManager(domain_store)
    operation_pacer = OperationPacer(
        os.getenv("ACCOUNT_OPERATION_DELAY_MIN", "15"),
        os.getenv("ACCOUNT_OPERATION_DELAY_MAX", "30"),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await account_store.load()
        await manager.load()
        await domain_store.load()
        app.state.registration_manager = manager
        app.state.account_store = account_store
        app.state.domain_automation_store = domain_store
        app.state.domain_automation_manager = domain_manager
        # Store pool and stats for access in routes
        app.state.account_pool = pool
        app.state.statistics = stats
        await domain_manager.start_renewal_scheduler()
        try:
            yield
        finally:
            await domain_manager.stop_renewal_scheduler()

    app = FastAPI(
        title="DigitalPlat Register Console",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    app.mount("/static", StaticFiles(directory=str(_PACKAGE_DIR / "static")), name="static")

    # ==================== Dashboard ====================

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        # Keep the original account-registration console as the primary entry
        # point.  Domain API automation is an additive module at
        # ``/domain-automation`` and must not replace the existing workflow.
        return TEMPLATES.TemplateResponse(
            request, "console.html", {"active_page": "console"}
        )

    @app.get("/domain-automation", response_class=HTMLResponse)
    async def domain_automation_dashboard(request: Request):
        return TEMPLATES.TemplateResponse(
            request, "domain_automation.html", {"active_page": "domain"}
        )
    
    # ==================== V2 API (Account Pool, Statistics, Logs) ====================
    
    if enable_v2_api:
        # Include the new API router for account pool, statistics, and logs
        v2_router = create_api_router(pool, stats)
        app.include_router(v2_router, prefix="/api/v2")
        
        # Migration endpoint
        @app.post("/api/v2/migrate")
        async def trigger_migration():
            """Trigger migration from legacy JSON to new SQLite pool"""
            try:
                from .core.bridge import AccountPoolBridge
                bridge = AccountPoolBridge(account_store, pool_db_path, stats_db_path)
                result = bridge.migrate_from_store()
                return {"status": "completed", **result}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))
        
        # V2 Dashboard page
        @app.get("/pool-dashboard", response_class=HTMLResponse)
        async def pool_dashboard(request: Request):
            return TEMPLATES.TemplateResponse(
                request, "pool.html", {"active_page": "pool"}
            )

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        return {
            "ok": True,
            "active_jobs": list(manager._active_job_ids),
            "batch_jobs": len([j for j in account_store.get_all_batch_jobs() if j.status == "running"]),
            "domain_jobs": domain_manager.overview()["stats"]["running_jobs"],
        }

    # ==================== Overview & Stats ====================

    @app.get("/api/overview")
    async def overview() -> Dict[str, Any]:
        return {
            **manager.overview(),
            "batch_jobs": [j.to_dict() for j in account_store.get_all_batch_jobs()[:10]],
        }

    # ==================== Legacy Single Registration ====================

    @app.post("/api/jobs", status_code=status.HTTP_202_ACCEPTED)
    async def start_registration(response: Response) -> Dict[str, Any]:
        try:
            job = await manager.start_single()
            await operation_pacer.wait()
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        response.headers["Location"] = f"/api/jobs/{job.id}"
        return job.snapshot()

    @app.get("/api/jobs/{job_id}")
    async def job_detail(job_id: str) -> Dict[str, Any]:
        job = manager.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Registration job not found")
        return job.snapshot()

    # ==================== Batch Registration ====================

    @app.post("/api/batch", status_code=status.HTTP_202_ACCEPTED)
    async def start_batch(request: Dict[str, Any]) -> Dict[str, Any]:
        """Start a batch registration job.
        
        Request body:
        {
            "count": 5,
            "referral_code": "abc123",
            "username_prefix": "myuser",
            "delay": 5.0,
            "max_concurrent": 1,
            "turnstile_sitekey": "optional sitekey override",
            "turnstile_endpoint": "optional endpoint override"
        }
        """
        count = request.get("count", 1)
        if not isinstance(count, int) or count < 1 or count > 100:
            raise HTTPException(status_code=400, detail="Count must be between 1 and 100")
        
        try:
            batch_job = await manager.start_batch(
                count=count,
                referral_code=request.get("referral_code", DEFAULT_REFERRAL_CODE),
                username_prefix=request.get("username_prefix"),
                delay=request.get("delay", 15.0),
                delay_max=request.get("delay_max", 30.0),
                max_concurrent=request.get("max_concurrent", 1),
                turnstile_sitekey=request.get("turnstile_sitekey"),
                turnstile_endpoint=request.get("turnstile_endpoint"),
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        
        return {
            "batch_job_id": batch_job.id,
            "status": batch_job.status,
            "total_accounts": batch_job.total_accounts,
            "message": f"Batch job created with {count} accounts",
        }

    @app.get("/api/batch/{batch_job_id}")
    async def batch_job_detail(batch_job_id: str) -> Dict[str, Any]:
        job = account_store.get_batch_job(batch_job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Batch job not found")
        
        # Include account details with password for active accounts
        accounts = []
        for aid in job.account_ids:
            account = account_store.get_account(aid)
            if account:
                # Include password for active accounts so users can use them
                account_data = account.to_dict() if account.status == AccountStatus.ACTIVE else account.to_safe_dict()
                account_data["progress"] = manager.account_progress(account)
                accounts.append(account_data)
        
        return {
            **job.to_dict(),
            "accounts": accounts,
        }

    @app.get("/api/batch")
    async def list_batch_jobs() -> Dict[str, Any]:
        jobs = account_store.get_all_batch_jobs()
        return {
            "total": len(jobs),
            "jobs": [j.to_dict() for j in jobs],
        }

    # ==================== Account Management ====================

    @app.get("/api/accounts")
    async def list_accounts(
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List accounts with optional filtering."""
        if status:
            try:
                account_status = AccountStatus(status)
                accounts = account_store.get_accounts_by_status(account_status)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
        else:
            accounts = account_store.get_all_accounts()
        
        total = len(accounts)
        accounts = accounts[offset:offset + limit]
        
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "accounts": [a.to_safe_dict() for a in accounts],
        }

    @app.get("/api/accounts/search")
    async def search_accounts(q: str) -> Dict[str, Any]:
        """Search accounts by username, email, or fullname."""
        accounts = account_store.search_accounts(q)
        return {
            "query": q,
            "total": len(accounts),
            "accounts": [a.to_safe_dict() for a in accounts],
        }

    @app.get("/api/accounts/stats")
    async def account_stats() -> Dict[str, Any]:
        """Get account statistics."""
        return account_store.count_accounts()

    @app.get("/api/accounts/{account_id}")
    async def get_account(account_id: str) -> Dict[str, Any]:
        """Get account details (with password for management)."""
        account = account_store.get_account(account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        return account.to_dict()

    @app.get("/api/accounts/{account_id}/safe")
    async def get_account_safe(account_id: str) -> Dict[str, Any]:
        """Get account details (safe version without password)."""
        account = account_store.get_account(account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        return account.to_safe_dict()

    @app.get("/api/accounts/{account_id}/progress")
    async def get_account_progress(account_id: str) -> Dict[str, Any]:
        """Get real-time registration progress for an account."""
        account = account_store.get_account(account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        return manager.account_progress(account)

    @app.put("/api/accounts/{account_id}")
    async def update_account(account_id: str, request: Dict[str, Any]) -> Dict[str, Any]:
        """Update account fields."""
        # Prevent updating sensitive fields directly
        forbidden_fields = {"id", "created_at", "status"}
        updates = {k: v for k, v in request.items() if k not in forbidden_fields}
        
        account = account_store.update_account(account_id, **updates)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        
        await account_store.save()
        return account.to_safe_dict()

    @app.delete("/api/accounts/{account_id}")
    async def delete_account(account_id: str) -> Dict[str, Any]:
        """Delete an account."""
        if not account_store.delete_account(account_id):
            raise HTTPException(status_code=404, detail="Account not found")
        
        await account_store.save()
        return {"message": "Account deleted", "account_id": account_id}

    @app.post("/api/accounts/bulk-delete")
    async def bulk_delete_accounts(request: Dict[str, Any]) -> Dict[str, Any]:
        """Delete multiple accounts."""
        account_ids = request.get("account_ids", [])
        if not account_ids:
            raise HTTPException(status_code=400, detail="No account_ids provided")
        
        deleted = account_store.delete_accounts(account_ids)
        await account_store.save()
        
        return {
            "message": f"Deleted {deleted} accounts",
            "deleted": deleted,
            "requested": len(account_ids),
        }

    @app.post("/api/accounts/export")
    async def export_accounts(request: Dict[str, Any]) -> Response:
        """Export accounts as JSON."""
        account_ids = request.get("account_ids")
        
        if account_ids:
            accounts = []
            for aid in account_ids:
                account = account_store.get_account(aid)
                if account:
                    accounts.append(account.to_dict())
        else:
            accounts = [a.to_dict() for a in account_store.get_all_accounts()]
        
        export_data = {
            "exported_at": _timestamp(),
            "total": len(accounts),
            "accounts": accounts,
        }
        
        return Response(
            content=json.dumps(export_data, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=accounts_export.json"},
        )

    # ==================== Manual Account Creation ====================

    @app.post("/api/accounts", status_code=status.HTTP_201_CREATED)
    async def create_account(request: Dict[str, Any]) -> Dict[str, Any]:
        """Manually create an account entry (without registering)."""
        username = request.get("username")
        email = request.get("email")
        
        if not username or not email:
            raise HTTPException(status_code=400, detail="username and email are required")
        
        account = Account(
            username=username,
            email=email,
            password=request.get("password", ""),
            fullname=request.get("fullname"),
            phone=request.get("phone"),
            address_line1=request.get("address_line1"),
            address_line2=request.get("address_line2"),
            city=request.get("city"),
            state=request.get("state"),
            postal_code=request.get("postal_code"),
            country=request.get("country", "US"),
            referral_code=request.get("referral_code", DEFAULT_REFERRAL_CODE),
            status=AccountStatus.PENDING,
        )
        
        account_store.create_account(account)
        await account_store.save()
        
        return account.to_safe_dict()

    @app.post("/api/accounts/{account_id}/register")
    async def register_existing_account(account_id: str) -> Dict[str, Any]:
        """Register an existing account entry through DigitalPlat."""
        account = account_store.get_account(account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        
        if account.status == AccountStatus.ACTIVE:
            raise HTTPException(status_code=400, detail="Account is already active")
        
        await operation_pacer.wait()
        account.mark_registering()
        account.metadata["steps"] = []
        account.metadata["current_step"] = REGISTRATION_STEP_ORDER[0]
        await account_store.save()
        
        # Start async registration
        asyncio.create_task(_register_account_async(account_id))
        
        return {
            "message": "Registration started",
            "account_id": account_id,
            "status": "registering",
        }

    async def _register_account_async(account_id: str) -> None:
        """Register existing account asynchronously."""
        account = account_store.get_account(account_id)
        if not account:
            return

        def on_step_complete(step: StepResult) -> None:
            step_data = {
                "name": step.name,
                "success": step.success,
                "duration": step.duration,
                "message": step.message,
                "error": step.error,
                "timestamp": step.timestamp.isoformat(),
            }
            account.metadata.setdefault("steps", []).append(step_data)
            try:
                current_index = REGISTRATION_STEP_ORDER.index(step.name)
            except ValueError:
                current_index = -1
            account.metadata["current_step"] = (
                REGISTRATION_STEP_ORDER[current_index + 1]
                if current_index + 1 < len(REGISTRATION_STEP_ORDER)
                else None
            )
            asyncio.create_task(account_store.save())
        
        try:
            result = await register_with_defaults(
                username=account.username,
                email=account.email if account.email else None,
                fullname=account.fullname,
                phone=account.phone,
                password=account.password,
                address_line1=account.address_line1,
                city=account.city,
                state=account.state,
                postal_code=account.postal_code,
                country=account.country,
                referral_code=account.referral_code or DEFAULT_REFERRAL_CODE,
                on_step_complete=on_step_complete,
            )
            
            if result.success:
                if result.email:
                    account.email = result.email
                if result.username:
                    account.username = result.username
                if result.password:
                    account.password = result.password
                account.mark_active()
            else:
                failed_step = account.metadata.get("current_step")
                account.mark_failed(result.error or "Unknown error", failed_step or result.error_stage)
                if failed_step and not any(
                    step.get("name") == failed_step and step.get("success") is False
                    for step in account.metadata.get("steps", [])
                ):
                    account.metadata.setdefault("steps", []).append({
                        "name": failed_step,
                        "success": False,
                        "duration": None,
                        "message": result.error or "注册步骤失败",
                        "error": result.error,
                        "timestamp": _timestamp(),
                    })
                
        except Exception as e:
            account.mark_failed(str(e))

        account.metadata["current_step"] = None
        await account_store.save()

    # ==================== Account-based Domain Registration (Legacy) ====================

    @app.get("/api/domains")
    async def list_domains() -> Dict[str, Any]:
        """List domains registered through the original account workflow."""
        accounts = account_store.get_accounts_by_status(AccountStatus.ACTIVE)
        domains = []
        for account in accounts:
            for domain in account.metadata.get("domains", []):
                domains.append({
                    "username": account.username,
                    "domain": domain.get("domain"),
                    "registered_at": domain.get("registered_at"),
                    "nameservers": domain.get("nameservers", []),
                })
        return {"total": len(domains), "domains": domains}

    @app.post("/api/domains/register")
    async def register_domain(request: Dict[str, Any]) -> Dict[str, Any]:
        """Register a domain through the original account/browser workflow."""
        from .services.domain_registrar import register_domain_with_defaults

        username = request.get("username")
        password = request.get("password")
        domain_prefix = request.get("domain_prefix")
        domain_suffix = request.get("domain_suffix", "dpdns.org")
        nameservers = request.get("nameservers")
        proxy = request.get("proxy")

        if not username or not domain_prefix:
            raise HTTPException(
                status_code=400,
                detail="username and domain_prefix are required",
            )

        if not password:
            accounts = account_store.get_accounts_by_status(AccountStatus.ACTIVE)
            password = next(
                (account.password for account in accounts if account.username == username),
                None,
            )
            if not password:
                raise HTTPException(
                    status_code=404,
                    detail=f"Account '{username}' not found or has no saved password",
                )

        await operation_pacer.wait()
        result = await register_domain_with_defaults(
            username=username,
            password=password,
            domain_prefix=domain_prefix,
            domain_suffix=domain_suffix,
            nameservers=nameservers,
            proxy=proxy,
        )

        if result.success:
            accounts = account_store.get_accounts_by_status(AccountStatus.ACTIVE)
            for account in accounts:
                if account.username == username:
                    account.metadata.setdefault("domains", []).append({
                        "domain": f"{domain_prefix}.{domain_suffix}",
                        "registered_at": result.registered_at,
                        "nameservers": result.nameservers,
                    })
                    await account_store.save()
                    break

        return {
            "success": result.success,
            "domain": result.domain,
            "message": result.message,
            "steps": result.steps,
            "error": result.error,
        }

    @app.post("/api/domains/check")
    async def check_domain(request: Dict[str, Any]) -> Dict[str, Any]:
        """Check availability through the original account/browser workflow."""
        from .services.domain_registrar import DomainRegistrar, DomainRegistrationConfig

        username = request.get("username")
        password = request.get("password")
        domain_prefix = request.get("domain_prefix")
        domain_suffix = request.get("domain_suffix", "dpdns.org")

        if not username or not domain_prefix:
            raise HTTPException(
                status_code=400,
                detail="username and domain_prefix are required",
            )

        if not password:
            accounts = account_store.get_accounts_by_status(AccountStatus.ACTIVE)
            password = next(
                (account.password for account in accounts if account.username == username),
                None,
            )
            if not password:
                raise HTTPException(
                    status_code=404,
                    detail=f"Account '{username}' not found or has no saved password",
                )

        registrar = DomainRegistrar(DomainRegistrationConfig(
            username=username,
            password=password,
            domain_prefix=domain_prefix,
            domain_suffix=domain_suffix,
        ))
        await registrar._init_browser(headless=True)

        try:
            login_result = await registrar.login()
            if not login_result.success:
                return {
                    "available": False,
                    "domain": f"{domain_prefix}.{domain_suffix}",
                    "message": f"Login failed: {login_result.error}",
                }
            check_result = await registrar.check_domain_availability()
            return {
                "available": check_result.available,
                "domain": check_result.domain,
                "message": check_result.message,
            }
        finally:
            await registrar._close_browser()

    # ==================== Domain API Automation ====================

    @app.get("/api/domain-automation")
    async def domain_automation_overview() -> Dict[str, Any]:
        return domain_manager.overview()

    @app.post("/api/domain-automation/tokens", status_code=status.HTTP_201_CREATED)
    async def create_domain_token(request: Dict[str, Any]) -> Dict[str, Any]:
        try:
            token = domain_manager.validate_token(request.get("token"))
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        name = str(request.get("name") or f"Token {len(domain_store.tokens) + 1}").strip()[:80]
        if any(item.token == token for item in domain_store.tokens.values()):
            raise HTTPException(status_code=409, detail="This API Token already exists")
        record = APITokenRecord(name=name, token=token)
        domain_store.tokens[record.id] = record
        await domain_store.save()
        return record.safe_dict()

    @app.delete("/api/domain-automation/tokens/{token_id}")
    async def delete_domain_token(token_id: str) -> Dict[str, Any]:
        if not domain_store.tokens.pop(token_id, None):
            raise HTTPException(status_code=404, detail="API Token not found")
        await domain_store.save()
        return {"deleted": token_id}

    @app.post("/api/domain-automation/tokens/{token_id}/test")
    async def test_domain_token(token_id: str) -> Dict[str, Any]:
        try:
            return await domain_manager.test_token(token_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="API Token not found") from error

    @app.put("/api/domain-automation/cloudflare")
    async def save_cloudflare_settings(request: Dict[str, Any]) -> Dict[str, Any]:
        try:
            data = domain_manager.normalize_cloudflare_settings(request)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        domain_store.cloudflare = CloudflareSettings(**data)
        await domain_store.save()
        return domain_store.cloudflare.safe_dict()

    @app.delete("/api/domain-automation/cloudflare")
    async def delete_cloudflare_settings() -> Dict[str, Any]:
        domain_store.cloudflare = None
        await domain_store.save()
        return {"deleted": True}

    @app.post("/api/domain-automation/cloudflare/test")
    async def test_cloudflare_settings() -> Dict[str, Any]:
        try:
            return await domain_manager.test_cloudflare()
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.put("/api/domain-automation/renewal")
    async def save_renewal_settings(request: Dict[str, Any]) -> Dict[str, Any]:
        try:
            data = domain_manager.normalize_renewal_settings(request)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        for key, value in data.items():
            setattr(domain_store.renewal, key, value)
        await domain_store.save()
        return domain_store.renewal.safe_dict()

    @app.post("/api/domain-automation/renewal/run")
    async def run_domain_renewal(request: Dict[str, Any]) -> Dict[str, Any]:
        return await domain_manager.run_renewal(force=bool(request.get("force", False)))

    @app.post("/api/domain-automation/domains/{domain}/cloudflare")
    async def host_domain_on_cloudflare(domain: str) -> Dict[str, Any]:
        try:
            return await domain_manager.host_domain_on_cloudflare(domain)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.delete("/api/domain-automation/domains/{domain}")
    async def delete_domain_record(domain: str, delete_cf_zone: bool = False) -> Dict[str, Any]:
        try:
            return await domain_manager.delete_domain(domain, delete_cf_zone=delete_cf_zone)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Domain record not found") from error
        except Exception as error:
            raise HTTPException(status_code=500, detail=str(error)) from error

    @app.post("/api/domain-automation/domains/bulk-delete")
    async def bulk_delete_domain_records(request: Dict[str, Any]) -> Dict[str, Any]:
        domains = request.get("domains", [])
        delete_cf_zone = bool(request.get("delete_cf_zone", False))
        if not isinstance(domains, list) or not domains:
            raise HTTPException(status_code=400, detail="domains list is required")
        return await domain_manager.bulk_delete_domains(domains, delete_cf_zone=delete_cf_zone)

    @app.post("/api/domain-automation/domains/cleanup")
    async def cleanup_invalid_domain_records() -> Dict[str, Any]:
        return await domain_manager.cleanup_invalid_domains()

    @app.post("/api/domain-automation/domains/{domain}/refresh")
    async def refresh_domain_status(domain: str) -> Dict[str, Any]:
        try:
            return await domain_manager.refresh_domain_status(domain)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(status_code=500, detail=str(error)) from error

    @app.patch("/api/domain-automation/domains/{domain}/nameservers")
    async def update_domain_nameservers(domain: str, request: Dict[str, Any]) -> Dict[str, Any]:
        nameservers = request.get("nameservers", [])
        if not isinstance(nameservers, list) or len(nameservers) < 2:
            raise HTTPException(status_code=400, detail="At least two nameservers are required")
        try:
            return await domain_manager.update_domain_nameservers(domain, nameservers)
        except (ValueError, DigitalPlatAPIError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(status_code=500, detail=str(error)) from error

    @app.post("/api/domain-automation/domains/sync")
    async def sync_digitalplat_domains() -> Dict[str, Any]:
        return await domain_manager.sync_domains()

    @app.post("/api/domain-automation/subscriptions", status_code=status.HTTP_201_CREATED)
    async def create_prefix_subscription(request: Dict[str, Any]) -> Dict[str, Any]:
        try:
            data = domain_manager.normalize_subscription(request)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        subscription = PrefixSubscription(**data)
        domain_store.subscriptions[subscription.id] = subscription
        await domain_store.save()
        return subscription.to_dict()

    @app.delete("/api/domain-automation/subscriptions/{subscription_id}")
    async def delete_prefix_subscription(subscription_id: str) -> Dict[str, Any]:
        if not domain_store.subscriptions.pop(subscription_id, None):
            raise HTTPException(status_code=404, detail="Prefix subscription not found")
        await domain_store.save()
        return {"deleted": subscription_id}

    @app.post("/api/domain-automation/jobs", status_code=status.HTTP_202_ACCEPTED)
    async def start_domain_registration_job(request: Dict[str, Any]) -> Dict[str, Any]:
        try:
            job = await domain_manager.start_job(
                subscription_id=str(request.get("subscription_id", "")),
                target_count=request.get("target_count", 1),
                token_ids=request.get("token_ids"),
                max_attempts=request.get("max_attempts"),
                delay_min_seconds=request.get("delay_min_seconds", 20),
                delay_max_seconds=request.get("delay_max_seconds", 45),
            )
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return job.to_dict()

    @app.get("/api/domain-automation/jobs/{job_id}")
    async def domain_registration_job_detail(job_id: str) -> Dict[str, Any]:
        job = domain_store.jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Domain registration job not found")
        return job.to_dict()

    return app


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the DigitalPlat web console")
    parser.add_argument("--host", default=os.getenv("WEB_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("WEB_PORT", "8400")))
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

