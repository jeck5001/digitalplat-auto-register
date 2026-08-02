"""Web management console for batch DigitalPlat registrations and account management."""

import argparse
import asyncio
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
from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import HTMLResponse

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

    # ==================== Dashboard ====================

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        # Keep the original account-registration console as the primary entry
        # point.  Domain API automation is an additive module at
        # ``/domain-automation`` and must not replace the existing workflow.
        return DASHBOARD_HTML

    @app.get("/domain-automation", response_class=HTMLResponse)
    async def domain_automation_dashboard() -> str:
        return DOMAIN_AUTOMATION_HTML
    
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
        async def pool_dashboard() -> str:
            return POOL_DASHBOARD_HTML

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


# ==================== HTML Templates ====================

POOL_DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>账户池管理 - DigitalPlat</title>
  <style>
    :root { --bg:#0f172a; --panel:#1e293b; --ink:#f1f5f9; --muted:#94a3b8; 
            --green:#10b981; --red:#ef4444; --blue:#3b82f6; --amber:#f59e0b; }
    body { font-family: system-ui, sans-serif; margin:0; background:var(--bg); color:var(--ink); }
    .header { padding: 16px 24px; background:var(--panel); border-bottom: 1px solid #334155; }
    .header h1 { margin:0; font-size: 20px; }
    .nav { display:flex; gap:16px; padding:12px 24px; background:var(--panel); border-bottom:1px solid #334155; }
    .nav a { color:var(--muted); text-decoration:none; padding:6px 12px; border-radius:6px; }
    .nav a:hover, .nav a.active { color:var(--ink); background:#334155; }
    .container { max-width:1200px; margin:24px auto; padding:0 24px; }
    .stats-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:16px; margin-bottom:24px; }
    .stat-card { background:var(--panel); padding:20px; border-radius:12px; border:1px solid #334155; }
    .stat-value { font-size:32px; font-weight:700; margin:8px 0; }
    .stat-label { color:var(--muted); font-size:14px; }
    .green { color:var(--green); }
    .red { color:var(--red); }
    .blue { color:var(--blue); }
    table { width:100%; border-collapse:collapse; margin-top:16px; }
    th, td { padding:12px; text-align:left; border-bottom:1px solid #334155; }
    th { color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase; }
    .btn { padding:8px 16px; border:none; border-radius:6px; cursor:pointer; font-size:14px; font-weight:500; }
    .btn-primary { background:var(--blue); color:#fff; }
    .btn-danger { background:var(--red); color:#fff; }
    .status-badge { padding:4px 10px; border-radius:12px; font-size:12px; font-weight:500; }
    .status-active { background:rgba(16,185,129,0.2); color:var(--green); }
    .status-suspended { background:rgba(239,68,68,0.2); color:var(--red); }
    .tabs { display:flex; gap:8px; margin-bottom:16px; }
    .tab { padding:8px 16px; border-radius:6px; cursor:pointer; background:var(--panel); border:1px solid #334155; }
    .tab.active { background:var(--blue); border-color:var(--blue); }
  </style>
</head>
<body>
  <div class="header">
    <h1>🔐 DigitalPlat 账户池管理</h1>
  </div>
  <div class="nav">
    <a href="/">注册控制台</a>
    <a href="/domain-automation">域名自动化</a>
    <a href="/pool-dashboard" class="active">账户池</a>
  </div>
  <div class="container">
    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-label">总账户数</div>
        <div class="stat-value blue" id="total-accounts">-</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">可用账户</div>
        <div class="stat-value green" id="available-accounts">-</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">健康度</div>
        <div class="stat-value" id="pool-health">-</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">今日注册</div>
        <div class="stat-value" id="today-registrations">-</div>
      </div>
    </div>
    
    <div class="tabs">
      <div class="tab active" onclick="showTab('accounts')">账户列表</div>
      <div class="tab" onclick="showTab('stats')">统计面板</div>
      <div class="tab" onclick="showTab('logs')">日志查看</div>
      <div class="tab" onclick="showTab('health')">健康检查</div>
    </div>
    
    <div id="accounts-panel">
      <button class="btn btn-primary" onclick="migrate()">🔄 从旧系统迁移数据</button>
      <table>
        <thead>
          <tr><th>ID</th><th>用户名</th><th>邮箱</th><th>状态</th><th>使用次数</th><th>成功率</th><th>操作</th></tr>
        </thead>
        <tbody id="accounts-table"></tbody>
      </table>
    </div>
    
    <div id="stats-panel" style="display:none">
      <div class="stats-grid">
        <div class="stat-card">
          <div class="stat-label">总注册</div>
          <div class="stat-value" id="stat-total">-</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">成功率</div>
          <div class="stat-value green" id="stat-rate">-</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">域名注册</div>
          <div class="stat-value blue" id="stat-domains">-</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">平均耗时</div>
          <div class="stat-value" id="stat-avgtime">-</div>
        </div>
      </div>
    </div>
    
    <div id="logs-panel" style="display:none">
      <table>
        <thead>
          <tr><th>时间</th><th>级别</th><th>消息</th><th>上下文</th></tr>
        </thead>
        <tbody id="logs-table"></tbody>
      </table>
    </div>
    
    <div id="health-panel" style="display:none">
      <table>
        <thead>
          <tr><th>账户ID</th><th>健康</th><th>问题</th><th>成功率</th></tr>
        </thead>
        <tbody id="health-table"></tbody>
      </table>
    </div>
  </div>
  
  <script>
    async function loadStats() {
      const r = await fetch('/api/v2/pool');
      const d = await r.json();
      document.getElementById('total-accounts').textContent = d.total_accounts || 0;
      document.getElementById('available-accounts').textContent = d.available_accounts || 0;
      document.getElementById('pool-health').textContent = (d.pool_health * 100).toFixed(0) + '%';
      document.getElementById('pool-health').className = 'stat-value ' + (d.pool_health > 0.7 ? 'green' : d.pool_health > 0.3 ? '' : 'red');
    }
    
    async function loadAccounts() {
      const r = await fetch('/api/v2/pool/accounts');
      const d = await r.json();
      const tbody = document.getElementById('accounts-table');
      tbody.innerHTML = d.map(a => '<tr><td>' + a.id.slice(0,12) + '...</td><td>' + a.username + '</td><td>' + a.email + '</td><td><span class="status-badge status-' + a.status + '">' + a.status + '</span></td><td>' + a.total_uses + '</td><td>' + (a.success_rate * 100).toFixed(0) + '%</td><td><button class="btn btn-danger" onclick="deleteAccount(\\'' + a.id + '\\')">删除</button></td></tr>').join('');
    }
    
    async function loadDashboardStats() {
      const r = await fetch('/api/v2/stats');
      const d = await r.json();
      document.getElementById('stat-total').textContent = d.total_registrations;
      document.getElementById('stat-rate').textContent = (d.registration_success_rate * 100).toFixed(0) + '%';
      document.getElementById('stat-domains').textContent = d.total_domains_registered;
      document.getElementById('stat-avgtime').textContent = d.avg_registration_duration.toFixed(1) + 's';
    }
    
    async function loadLogs() {
      const r = await fetch('/api/v2/logs?limit=50');
      const d = await r.json();
      const tbody = document.getElementById('logs-table');
      tbody.innerHTML = d.map(l => '<tr><td>' + l.timestamp.slice(11,19) + '</td><td>' + l.level + '</td><td>' + l.message.slice(0,50) + '</td><td>' + JSON.stringify(l.context || {}).slice(0,30) + '</td></tr>').join('');
    }
    
    async function loadHealth() {
      const r = await fetch('/api/v2/pool/health');
      const d = await r.json();
      const tbody = document.getElementById('health-table');
      tbody.innerHTML = Object.entries(d).map(([id,h]) => '<tr><td>' + id.slice(0,12) + '...</td><td>' + (h.healthy ? '✅' : '❌')</td><td>' + (h.issues.join(', ') || '无') + '</td><td>' + (h.success_rate * 100).toFixed(0) + '%</td></tr>').join('');
    }
    
    async function migrate() {
      if (!confirm('确定要从旧系统迁移数据吗？')) return;
      const r = await fetch('/api/v2/migrate', {method:'POST'});
      const d = await r.json();
      alert('迁移完成: ' + JSON.stringify(d));
      loadStats(); loadAccounts();
    }
    
    async function deleteAccount(id) {
      if (!confirm('确定要删除该账户吗？')) return;
      await fetch('/api/v2/pool/accounts/' + id, {method:'DELETE'});
      loadAccounts();
    }
    
    function showTab(name) {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      event.target.classList.add('active');
      document.getElementById('accounts-panel').style.display = name === 'accounts' ? '' : 'none';
      document.getElementById('stats-panel').style.display = name === 'stats' ? '' : 'none';
      document.getElementById('logs-panel').style.display = name === 'logs' ? '' : 'none';
      document.getElementById('health-panel').style.display = name === 'health' ? '' : 'none';
      if (name === 'stats') loadDashboardStats();
      if (name === 'logs') loadLogs();
      if (name === 'health') loadHealth();
    }
    
    loadStats(); loadAccounts();
    setInterval(loadStats, 30000);
  </script>
</body>
</html>"""


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the DigitalPlat web console")
    parser.add_argument("--host", default=os.getenv("WEB_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("WEB_PORT", "8400")))
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DigitalPlat 控制台</title>
  <style>
    :root {
      --ink:#1a2332; --muted:#64748b; --line:#e2e8f0; --paper:#f8fafc;
      --panel:#ffffff; --green:#059669; --red:#dc2626; --amber:#d97706;
      --teal:#0891b2; --blue:#2563eb; --purple:#7c3aed;
    }
    @keyframes slideIn {
      from { transform: translateX(100%); opacity: 0; }
      to { transform: translateX(0); opacity: 1; }
    }
    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.5; }
    }
    * { box-sizing:border-box; }
    body { margin:0; color:var(--ink); background:var(--paper); font-family:"Avenir Next","PingFang SC","Noto Sans CJK SC",sans-serif; }
    main { max-width:1280px; margin:0 auto; padding:24px 20px 48px; }
    header { display:flex; align-items:flex-end; justify-content:space-between; gap:24px; border-bottom:2px solid var(--ink); padding-bottom:18px; flex-wrap:wrap; }
    h1 { margin:0; font-size:28px; font-weight:650; }
    .subtitle { color:var(--muted); font-size:14px; }
    nav { display:flex; gap:4px;margin-top:24px;border-bottom:2px solid var(--line); }
    nav button { border:none; background:none; padding:12px 20px; font:inherit; cursor:pointer; color:var(--muted);border-bottom:3px solid transparent;transition:all 0.2s; }
    nav button:hover { color:var(--ink);background:var(--paper); }
    nav button.active { color:var(--green); border-bottom-color:var(--green); font-weight:600; }
    .tab-content { display:none; padding-top:24px; }
    .tab-content.active { display:block; }
    .metrics { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:1px; background:var(--line); border:1px solid var(--line); margin:24px 0; }
    .metric { background:var(--panel); padding:18px; }
    .metric-label { color:var(--muted); font-size:12px; }
    .metric-value { font-size:28px; margin-top:8px; font-variant-numeric:tabular-nums; }
    .card { background:var(--panel); border:1px solid var(--line); margin-bottom:20px; }
    .card-header { padding:16px 20px; border-bottom:1px solid var(--line); font-weight:600; }
    .card-body { padding:20px; }
    .grid-2 { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
    @media (max-width:760px) { .grid-2 { grid-template-columns:1fr; } }
    label { display:block; color:var(--muted); font-size:13px; margin-bottom:6px; }
    input, select, textarea { width:100%; border:1px solid #abb5af; background:#fff; border-radius:4px; padding:10px 12px; color:var(--ink); font:inherit; margin-bottom:12px; }
    input:focus, select:focus, textarea:focus { outline:none; border-color:var(--green); }
    button.btn { border:0; border-radius:6px; padding:10px 20px; background:var(--green); color:#fff; font:inherit; font-weight:600; cursor:pointer; transition:all 0.2s; box-shadow:0 1px 3px rgba(0,0,0,0.1); }
    button.btn:hover { transform:translateY(-1px); box-shadow:0 4px 12px rgba(5,150,105,0.3); }
    button.btn:disabled { background:#94a3b8; cursor:not-allowed; transform:none; box-shadow:none; }
    button.btn-secondary { background:var(--panel); color:var(--ink); border:1px solid var(--line); }
    button.btn-secondary:hover { border-color:var(--green); color:var(--green); background:#f0fdf4; }
    .hint { font-size:12px; color:var(--muted); line-height:1.6; }
    table { width:100%; border-collapse:collapse; }
    th, td { padding:10px 12px; text-align:left; border-bottom:1px solid var(--line); font-size:13px; }
    th { background:var(--paper); font-weight:600; color:var(--muted); }
    tr:hover { background:var(--paper); }
    .badge { padding:3px 8px; border-radius:999px; font-size:11px; font-weight:600; white-space:nowrap; }
    .badge-running { background:#e0f1f1; color:var(--teal); animation:pulse 1.5s infinite; }
    .badge-succeeded, .badge-active { background:#def3e8; color:var(--green); }
    .badge-failed { background:#f8e2df; color:var(--red); }
    .badge-pending { background:#fef3c7; color:var(--amber); }
    .badge-registering { background:#dbeafe; color:var(--blue); animation:pulse 1.5s infinite; }
    .toolbar { display:flex; gap:10px; margin-bottom:16px; align-items:center; flex-wrap:wrap; }
    .search-box { flex:1; min-width:200px; }
    .progress-bar { height:8px; background:var(--line); border-radius:4px; overflow:hidden; margin-top:8px; }
    .progress-fill { height:100%; background:var(--green); transition:width 0.3s; }
    .steps { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
    .step { border:1px solid var(--line); padding:4px 8px; color:var(--muted); font-size:11px; border-radius:4px; display:flex; align-items:center; gap:4px; }
    .step.ok { border-color:#8dc8ad; color:var(--green); background:#f0fdf4; }
    .step.no { border-color:#df9991; color:var(--red); background:#fef2f2; }
    .step.current { border-color:var(--blue); color:var(--blue); background:#eff6ff; animation:pulse 1s infinite; }
    .account-actions { display:flex; gap:6px; }
    .account-actions button { padding:4px 8px; font-size:11px; }
    .modal-overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.5); z-index:100; align-items:center; justify-content:center; }
    .modal-overlay.active { display:flex; }
    .modal { background:#fff; border-radius:8px; padding:24px; max-width:500px; width:90%; max-height:80vh; overflow:auto; }
    .modal h3 { margin-top:0; }
    .modal-actions { display:flex; gap:10px; justify-content:flex-end; margin-top:20px; }
    /* Calm operations-workspace treatment: dense, readable, and one accent. */
    :root {
      --ink:#17211d; --muted:#68756f; --line:#dfe7e3; --paper:#f3f6f4;
      --panel:#ffffff; --green:#087f5b; --green-soft:#e7f5ef; --red:#c2413a;
      --amber:#a16207; --teal:#087f5b; --blue:#2563eb;
    }
    body { background:linear-gradient(180deg,#edf3f0 0,#f7f9f8 260px); min-height:100vh; }
    main { max-width:1440px; padding:0 28px 56px; }
    header { margin:0 -28px; padding:25px 28px 23px; align-items:center; border:0; background:#14231d; color:#fff; }
    h1 { font-size:25px; letter-spacing:-.02em; }
    header .subtitle { color:#aebdb6; margin-top:5px; }
    #updated { display:flex; align-items:center; gap:8px; }
    #updated::before { content:""; width:8px; height:8px; border-radius:50%; background:#42d39a; box-shadow:0 0 0 4px rgba(66,211,154,.13); }
    nav { margin:0 -28px 26px; padding:0 28px; background:#fff; border-bottom:1px solid var(--line); gap:22px; }
    nav button { padding:16px 2px 13px; border-bottom-width:2px; font-size:14px; }
    nav button:hover { background:transparent; }
    nav .module-link { margin-left:auto; align-self:center; padding:8px 13px; border-radius:7px; background:var(--green-soft); color:var(--green); font-size:13px; font-weight:650; text-decoration:none; transition:background .15s ease,transform .15s ease; }
    nav .module-link:hover { background:#d8eee4; transform:translateY(-1px); }
    .tab-content { padding-top:0; animation:contentIn .22s ease-out; }
    @keyframes contentIn { from { opacity:0; transform:translateY(4px); } to { opacity:1; transform:none; } }
    .metrics { border:0; border-bottom:1px solid var(--line); background:transparent; gap:0; margin:0 0 28px; }
    .metric { background:transparent; border-right:1px solid var(--line); padding:17px 22px 19px; }
    .metric:first-child { padding-left:0; }
    .metric:last-child { border-right:0; }
    .metric-label { font-size:12px; letter-spacing:.03em; }
    .metric-value { font-size:30px; font-weight:650; letter-spacing:-.04em; }
    .card { border:1px solid var(--line); border-radius:12px; overflow:hidden; box-shadow:0 8px 28px rgba(24,49,38,.035); }
    .card-header { padding:17px 20px; background:#fbfcfb; }
    .card-body { padding:18px 20px; }
    th { background:#f7f9f8; color:#63716a; font-size:11px; text-transform:uppercase; letter-spacing:.04em; }
    th, td { padding:12px 14px; }
    tbody tr { transition:background .15s ease; }
    .badge { padding:4px 9px; border-radius:6px; letter-spacing:.02em; }
    .badge-running, .badge-registering { animation:none; position:relative; padding-left:20px; }
    .badge-running::before, .badge-registering::before { content:""; position:absolute; left:8px; top:50%; width:6px; height:6px; margin-top:-3px; border-radius:50%; background:currentColor; animation:pulse 1.3s infinite; }
    input, select, textarea { border-color:#cfd9d4; border-radius:7px; padding:11px 12px; }
    input:focus, select:focus, textarea:focus { box-shadow:0 0 0 3px rgba(8,127,91,.1); }
    button.btn { border-radius:7px; box-shadow:none; }
    button.btn:hover { box-shadow:none; }
    .progress-bar { height:7px; background:#e7ece9; }
    .progress-fill { background:linear-gradient(90deg,#087f5b,#21a179); }
    .task-id { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; color:#405049; }
    .task-progress { min-width:150px; }
    .task-progress-line { display:flex; justify-content:space-between; color:var(--muted); font-size:11px; margin-bottom:6px; }
    .link-button { border:0; background:transparent; color:var(--green); padding:4px; cursor:pointer; font:inherit; font-weight:600; }
    .modal-overlay { background:rgba(15,29,23,.62); backdrop-filter:blur(4px); padding:24px; }
    .modal { border-radius:14px; padding:0; box-shadow:0 26px 80px rgba(9,24,17,.28); }
    .modal-wide { max-width:1040px; width:min(1040px,96vw); max-height:90vh; }
    .modal h3 { margin:0; padding:20px 24px; border-bottom:1px solid var(--line); font-size:18px; }
    #account-detail-content { padding:20px 24px; }
    .modal-actions { margin:0; padding:14px 24px; border-top:1px solid var(--line); }
    .batch-summary { display:grid; grid-template-columns:1.5fr repeat(4,1fr); gap:1px; background:var(--line); border:1px solid var(--line); border-radius:10px; overflow:hidden; margin-bottom:22px; }
    .batch-summary > div { background:#fff; padding:14px 16px; }
    .summary-label { color:var(--muted); font-size:11px; margin-bottom:5px; }
    .summary-value { font-size:19px; font-weight:650; font-variant-numeric:tabular-nums; }
    .account-progress { border-top:1px solid var(--line); }
    .account-progress:first-child { border-top:0; }
    .account-progress summary { list-style:none; display:grid; grid-template-columns:minmax(160px,1.4fr) 110px minmax(160px,1fr) 28px; gap:16px; align-items:center; padding:15px 2px; cursor:pointer; }
    .account-progress summary::-webkit-details-marker { display:none; }
    .account-name { min-width:0; }
    .account-name strong { display:block; overflow:hidden; text-overflow:ellipsis; }
    .account-email { color:var(--muted); font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; margin-top:3px; }
    .account-chevron { color:var(--muted); transition:transform .18s ease; }
    .account-progress[open] .account-chevron { transform:rotate(90deg); }
    .step-timeline { display:grid; grid-template-columns:repeat(6,1fr); padding:5px 6px 22px; }
    .timeline-step { position:relative; padding:31px 9px 0 0; min-width:0; }
    .timeline-step::before { content:""; position:absolute; left:8px; right:-8px; top:13px; height:2px; background:#dfe6e2; }
    .timeline-step:last-child::before { right:calc(100% - 8px); }
    .step-dot { position:absolute; left:0; top:5px; z-index:1; width:18px; height:18px; border-radius:50%; display:grid; place-items:center; background:#fff; border:2px solid #cbd5d0; color:#fff; font-size:10px; }
    .timeline-step.success::before { background:#65b999; }
    .timeline-step.success .step-dot { background:var(--green); border-color:var(--green); }
    .timeline-step.failed .step-dot { background:var(--red); border-color:var(--red); }
    .timeline-step.current .step-dot { border-color:var(--green); box-shadow:0 0 0 5px rgba(8,127,91,.11); animation:pulse 1.4s infinite; }
    .step-name { font-size:12px; font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .step-meta { color:var(--muted); font-size:11px; margin-top:4px; line-height:1.45; }
    .step-error { color:var(--red); font-size:12px; padding:0 6px 18px; }
    .empty-state { color:var(--muted); text-align:center; padding:28px 12px !important; }
    @media (max-width:900px) {
      .grid-2 { grid-template-columns:1fr; }
      .batch-summary { grid-template-columns:repeat(2,1fr); }
      .batch-summary > div:first-child { grid-column:1/-1; }
      .step-timeline { grid-template-columns:1fr; padding-left:9px; }
      .timeline-step { padding:4px 0 18px 36px; }
      .timeline-step::before { left:8px; right:auto; top:13px; bottom:-5px; width:2px; height:auto; }
      .timeline-step:last-child::before { display:none; }
    }
    @media (max-width:700px) {
      main { padding-left:14px; padding-right:14px; }
      header, nav { margin-left:-14px; margin-right:-14px; padding-left:14px; padding-right:14px; }
      nav { gap:14px; overflow-x:auto; }
      nav button { white-space:nowrap; }
      nav .module-link { margin-left:0; white-space:nowrap; }
      .metrics { grid-template-columns:repeat(2,1fr); }
      .metric { border-bottom:1px solid var(--line); }
      .account-progress summary { grid-template-columns:1fr auto; }
      .account-progress summary .task-progress { grid-column:1/-1; }
      .account-chevron { grid-column:2; grid-row:1; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>DigitalPlat 控制台</h1>
        <div class="subtitle">批量注册与账号管理</div>
      </div>
      <div id="updated" class="subtitle">正在连接...</div>
    </header>
    
    <nav>
      <button class="active" onclick="showTab('dashboard', this)">📊 概览</button>
      <button onclick="showTab('batch', this)">🚀 批量注册</button>
      <button onclick="showTab('accounts', this)">👤 账号管理</button>
      <button onclick="showTab('domains', this)">🌐 账号域名注册</button>
      <button onclick="showTab('history', this)">📜 任务记录</button>
      <a class="module-link" href="/domain-automation">API 域名自动注册 →</a>
    </nav>

    <!-- Dashboard Tab -->
    <div id="tab-dashboard" class="tab-content active">
      <div class="metrics">
        <div class="metric"><div class="metric-label">总账号数</div><div id="stat-total" class="metric-value">0</div></div>
        <div class="metric"><div class="metric-label">活跃</div><div id="stat-active" class="metric-value" style="color:var(--green)">0</div></div>
        <div class="metric"><div class="metric-label">注册中</div><div id="stat-registering" class="metric-value" style="color:var(--blue)">0</div></div>
        <div class="metric"><div class="metric-label">待注册</div><div id="stat-pending" class="metric-value" style="color:var(--amber)">0</div></div>
        <div class="metric"><div class="metric-label">失败</div><div id="stat-failed" class="metric-value" style="color:var(--red)">0</div></div>
        <div class="metric"><div class="metric-label">运行中任务</div><div id="stat-batch" class="metric-value">0</div></div>
      </div>
      
      <div class="grid-2">
        <div class="card">
          <div class="card-header">最近注册</div>
          <div class="card-body">
            <table id="recent-accounts-table">
              <thead><tr><th>用户名</th><th>邮箱</th><th>状态</th></tr></thead>
              <tbody><tr><td colspan="3" class="hint">加载中...</td></tr></tbody>
            </table>
          </div>
        </div>
        <div class="card">
          <div class="card-header">最近批量任务</div>
          <div class="card-body">
            <table id="recent-batches-table">
              <thead><tr><th>任务ID</th><th>总数</th><th>成功</th><th>状态</th></tr></thead>
              <tbody><tr><td colspan="4" class="hint">加载中...</td></tr></tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <!-- Batch Registration Tab -->
    <div id="tab-batch" class="tab-content">
      <div class="card">
        <div class="card-header">新建批量注册任务</div>
        <div class="card-body">
          <div class="grid-2">
            <div>
              <label>注册数量</label>
              <input type="number" id="batch-count" value="5" min="1" max="100">
            </div>
            <div>
              <label>邀请码</label>
              <input type="text" id="batch-referral" value="4qn8iw8r1o">
            </div>
            <div>
              <label>用户名前缀（可选）</label>
              <input type="text" id="batch-prefix" placeholder="留空则自动生成">
            </div>
              <div>
                <label>账号注册最小间隔（秒）</label>
                <input type="number" id="batch-delay" value="15" min="0" max="3600">
              </div>
              <div>
                <label>账号注册最大间隔（秒）</label>
                <input type="number" id="batch-delay-max" value="30" min="0" max="3600">
                <div class="hint">每个账号开始前随机等待，防止连续请求</div>
              </div>
              <div>
                <label>并发数</label>
                <select id="batch-concurrent">
                  <option value="1">1 (推荐)</option>
                  <option value="2">2</option>
                  <option value="3">3</option>
                </select>
                <div class="hint">有冷却间隔时按账号完成后串行执行；仅 0 秒间隔用于并发测试</div>
              </div>
            </div>
            
            <details style="margin:16px 0">
              <summary style="cursor:pointer;color:var(--muted);font-size:13px">🔧 Turnstile 配置（可选）</summary>
              <div class="grid-2" style="margin-top:12px">
                <div>
                  <label>Site Key</label>
                  <input type="text" id="batch-turnstile-sitekey" placeholder="留空则使用环境变量">
                </div>
                <div>
                  <label>Solver Endpoint</label>
                  <input type="text" id="batch-turnstile-endpoint" placeholder="留空则使用环境变量">
                </div>
              </div>
            </details>
            
            <button class="btn" id="start-batch-btn" onclick="startBatch()">开始批量注册</button>
          <p class="hint">批量创建账号，系统自动生成用户名和密码。注册间隔可避免被风控拦截。</p>
        </div>
      </div>
      
      <div id="active-batch-section" class="card" style="display:none;">
        <div class="card-header">当前批量任务</div>
        <div class="card-body" id="active-batch-info"></div>
      </div>
      
      <div class="card">
        <div class="card-header">历史批量任务</div>
        <div class="card-body">
          <table id="batch-history-table">
            <thead><tr><th>任务ID</th><th>总数</th><th>完成</th><th>成功</th><th>失败</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>
            <tbody><tr><td colspan="8" class="hint">加载中...</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Accounts Tab -->
    <div id="tab-accounts" class="tab-content">
      <div class="toolbar">
        <input type="text" class="search-box" id="account-search" placeholder="搜索用户名、邮箱或姓名...">
        <select id="account-filter" onchange="loadAccounts()">
          <option value="">全部状态</option>
          <option value="active">活跃</option>
          <option value="pending">待注册</option>
          <option value="registering">注册中</option>
          <option value="failed">失败</option>
        </select>
        <button class="btn btn-secondary" onclick="showAddAccountModal()">+ 添加账号</button>
        <button class="btn btn-secondary" onclick="exportAccounts()">导出</button>
        <button class="btn btn-danger" onclick="bulkDelete()">删除选中</button>
      </div>
      
      <div class="card">
        <table id="accounts-table">
          <thead>
            <tr>
              <th><input type="checkbox" id="select-all" onchange="toggleSelectAll()"></th>
              <th>ID</th>
              <th>用户名</th>
              <th>邮箱</th>
              <th>状态</th>
              <th>注册时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody><tr><td colspan="7" class="hint">加载中...</td></tr></tbody>
        </table>
      </div>
    </div>

    <!-- History Tab -->
    <div id="tab-history" class="tab-content">
      <div class="card">
        <div class="card-header">单次注册任务记录</div>
        <div class="card-body">
          <table id="history-table">
            <thead><tr><th>任务ID</th><th>账号ID</th><th>状态</th><th>结果</th><th>耗时</th><th>创建时间</th></tr></thead>
            <tbody><tr><td colspan="6" class="hint">加载中...</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Domain Registration Tab -->
    <div id="tab-domains" class="tab-content">
      <div class="card">
        <div class="card-header">通过已注册账号申请域名（原功能）</div>
        <div class="card-body">
          <p class="hint" style="margin-bottom:16px">
            支持 .dpdns.org / .us.kg / .xx.kg / .qzz.io / .qd.je 等免费后缀。每个账号限注册1个免费域名。
          </p>
          <div class="grid-2">
            <div>
              <label>选择账号 *</label>
              <select id="domain-account">
                <option value="">-- 选择已注册的账号 --</option>
              </select>
            </div>
            <div>
              <label>域名后缀</label>
              <select id="domain-suffix">
                <option value="dpdns.org">.dpdns.org (推荐)</option>
                <option value="us.kg">.us.kg ($3一次性)</option>
                <option value="xx.kg">.xx.kg ($3一次性)</option>
                <option value="qzz.io">.qzz.io</option>
                <option value="qd.je">.qd.je</option>
              </select>
            </div>
            <div>
              <label>域名前缀 *</label>
              <input type="text" id="domain-prefix" placeholder="例如: mysite">
            </div>
            <div>
              <label>Nameserver 1</label>
              <input type="text" id="domain-ns1" value="ns1.cloudflare.com">
            </div>
            <div>
              <label>Nameserver 2</label>
              <input type="text" id="domain-ns2" value="ns2.cloudflare.com">
            </div>
          </div>
          <div style="display:flex;gap:10px;margin-top:16px">
            <button class="btn" id="check-domain-btn" onclick="checkDomain()">检查可用性</button>
            <button class="btn" id="register-domain-btn" onclick="registerDomain()">注册域名</button>
          </div>
          <div id="domain-result" style="margin-top:12px"></div>
        </div>
      </div>

      <div class="card" style="margin-top:20px">
        <div class="card-header">已注册域名</div>
        <div class="card-body">
          <table id="domains-table">
            <thead><tr><th>域名</th><th>账号</th><th>注册时间</th><th>Nameservers</th></tr></thead>
            <tbody><tr><td colspan="4" class="hint">加载中...</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>
  </main>

  <!-- Add Account Modal -->
  <div class="modal-overlay" id="add-account-modal">
    <div class="modal">
      <h3>手动添加账号</h3>
      <label>用户名 *</label>
      <input type="text" id="new-username">
      <label>邮箱 *</label>
      <input type="email" id="new-email">
      <label>密码（可选）</label>
      <input type="text" id="new-password">
      <label>邀请码</label>
      <input type="text" id="new-referral" value="4qn8iw8r1o">
      <div class="modal-actions">
        <button class="btn btn-secondary" onclick="closeModal('add-account-modal')">取消</button>
        <button class="btn" onclick="createAccount()">创建</button>
      </div>
    </div>
  </div>

  <!-- Account Detail Modal -->
  <div class="modal-overlay" id="account-detail-modal">
    <div class="modal modal-wide">
      <h3 id="detail-modal-title">账号详情</h3>
      <div id="account-detail-content"></div>
      <div class="modal-actions">
        <button class="btn btn-secondary" onclick="closeModal('account-detail-modal')">关闭</button>
      </div>
    </div>
  </div>

  <script>
    const labels = { running:'运行中', succeeded:'成功', failed:'失败', pending:'待注册', registering:'注册中', active:'活跃', completed:'完成', paused:'暂停', expired:'过期' };
    
    function showTab(tabName, btn) {
      document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
      document.querySelectorAll('nav button').forEach(el => el.classList.remove('active'));
      document.getElementById('tab-' + tabName).classList.add('active');
      btn.classList.add('active');
      refresh();
      if (tabName === 'accounts') loadAccounts();
      if (tabName === 'batch') loadAccounts();
      if (tabName === 'domains') loadDomains();
    }
    
    function statusBadge(status) {
      const cls = status === 'active' || status === 'succeeded' ? 'active' :
                  status === 'failed' ? 'failed' :
                  status === 'registering' ? 'running' :
                  status === 'pending' ? 'pending' : 'running';
      return `<span class="badge badge-${cls}">${labels[status] || status}</span>`;
    }

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>'"]/g, char => ({
        '&':'&amp;', '<':'&lt;', '>':'&gt;', "'":'&#39;', '"':'&quot;'
      })[char]);
    }

    function formatTime(value) {
      if (!value) return '-';
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? escapeHtml(value) : date.toLocaleString();
    }

    function progressPercent(item) {
      return Math.min(100, Math.round(((item.completed_accounts || 0) / Math.max(item.total_accounts || 0, 1)) * 100));
    }
    
    async function refresh() {
      try {
        const response = await fetch('/api/overview');
        const data = await response.json();
        
        // Update dashboard stats
        const stats = data.account_overview?.accounts || {};
        document.getElementById('stat-total').textContent = stats.total || 0;
        document.getElementById('stat-active').textContent = stats.active || 0;
        document.getElementById('stat-registering').textContent = stats.registering || 0;
        document.getElementById('stat-pending').textContent = stats.pending || 0;
        document.getElementById('stat-failed').textContent = stats.failed || 0;
        document.getElementById('stat-batch').textContent = data.account_overview?.active_batch_jobs || 0;
        
        // Update updated time
        document.getElementById('updated').textContent = '已更新于 ' + new Date().toLocaleTimeString();
        
        // Render tables
        renderRecentAccounts(data);
        renderRecentBatches(data.batch_jobs || []);
        renderBatches(data.batch_jobs || []);
        renderJobs(data.jobs || []);
        
        // Auto-refresh accounts list if on accounts tab
        const accountsTab = document.getElementById('tab-accounts');
        if (accountsTab && accountsTab.classList.contains('active')) {
          loadAccounts();
        }
      } catch (error) {
        document.getElementById('updated').textContent = '服务暂不可用';
      }
    }

    function renderRecentBatches(batches) {
      const tbody = document.querySelector('#recent-batches-table tbody');
      if (!batches || !batches.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="empty-state">暂无批量任务</td></tr>';
        return;
      }
      tbody.innerHTML = batches.slice(0, 5).map(b => {
        const pct = progressPercent(b);
        return `<tr>
          <td><button class="link-button task-id" onclick="viewBatch('${b.id}')">${escapeHtml(b.id)}</button></td>
          <td>${b.total_accounts}</td>
          <td><span style="color:var(--green)">${b.successful_accounts}</span>${b.failed_accounts ? ` / <span style="color:var(--red)">${b.failed_accounts} 失败</span>` : ''}</td>
          <td><div class="task-progress"><div class="task-progress-line"><span>${statusBadge(b.status)}</span><span>${pct}%</span></div><div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div></div></td>
        </tr>`;
      }).join('');
    }
    
    async function renderRecentAccounts(data) {
      // Load from accounts API
      try {
        const r = await fetch('/api/accounts?limit=5');
        const result = await r.json();
        const tbody = document.querySelector('#recent-accounts-table tbody');
        if (!result.accounts || !result.accounts.length) {
          tbody.innerHTML = '<tr><td colspan="3" class="hint">暂无账号</td></tr>';
          return;
        }
        tbody.innerHTML = result.accounts.map(a => 
          `<tr><td>${a.username}</td><td>${a.email || '-'}</td><td>${statusBadge(a.status)}</td></tr>`
        ).join('');
      } catch (e) {
        const tbody = document.querySelector('#recent-accounts-table tbody');
        tbody.innerHTML = '<tr><td colspan="3" class="hint">加载失败</td></tr>';
      }
    }
    
    function renderBatches(batches) {
      const tbody = document.querySelector('#batch-history-table tbody');
      if (!batches || !batches.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="hint">暂无批量任务</td></tr>';
        document.getElementById('active-batch-section').style.display = 'none';
        return;
      }
      tbody.innerHTML = batches.map(b => {
        const pct = progressPercent(b);
        return `<tr>
          <td><button class="link-button task-id" onclick="viewBatch('${b.id}')">${escapeHtml(b.id)}</button></td>
          <td>${b.total_accounts}</td>
          <td><div class="task-progress"><div class="task-progress-line"><span>${b.completed_accounts} / ${b.total_accounts}</span><span>${pct}%</span></div><div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div></div></td>
          <td style="color:var(--green)">${b.successful_accounts}</td>
          <td style="color:var(--red)">${b.failed_accounts}</td>
          <td>${statusBadge(b.status)}</td>
          <td>${formatTime(b.created_at)}</td>
          <td><button class="btn btn-secondary" style="padding:4px 8px;font-size:11px" onclick="viewBatch('${b.id}')">详情</button></td>
        </tr>`;
      }).join('');
      
      // Show active batch if any
      const activeBatch = batches.find(b => b.status === 'running');
      const activeSection = document.getElementById('active-batch-section');
      if (activeBatch) {
        activeSection.style.display = 'block';
        const pct = Math.round((activeBatch.completed_accounts / activeBatch.total_accounts) * 100) || 0;
        document.getElementById('active-batch-info').innerHTML = `
          <div style="display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap">
            <div><div class="hint">任务 ID</div><strong class="task-id">${escapeHtml(activeBatch.id)}</strong></div>
            <button class="btn btn-secondary" onclick="viewBatch('${activeBatch.id}')">查看每个账号进度</button>
          </div>
          <p><strong>整体进度:</strong> ${activeBatch.completed_accounts} / ${activeBatch.total_accounts}</p>
          <div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div>
          <p class="hint">${pct}% · 成功 ${activeBatch.successful_accounts} · 失败 ${activeBatch.failed_accounts}</p>
        `;
      } else {
        activeSection.style.display = 'none';
      }
    }
    
    function renderJobs(jobs) {
      const tbody = document.querySelector('#history-table tbody');
      if (!jobs || !jobs.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="hint">暂无记录</td></tr>';
        return;
      }
      tbody.innerHTML = jobs.map(j => 
        `<tr>
          <td>${j.id}</td>
          <td>${j.account_id || '-'}</td>
          <td>${statusBadge(j.status)}</td>
          <td>${j.result ? (j.result.username || '-') : '-'}</td>
          <td>${j.result?.duration ? j.result.duration.toFixed(1) + 's' : '-'}</td>
          <td>${j.created_at}</td>
        </tr>`
      ).join('');
    }
    
    // Toast notification system
    function showToast(message, type = 'success') {
      const toast = document.createElement('div');
      toast.style.cssText = `
        position:fixed;top:20px;right:20px;padding:12px 20px;border-radius:8px;
        color:#fff;font-size:14px;z-index:9999;animation:slideIn 0.3s ease;
        box-shadow:0 4px 12px rgba(0,0,0,0.15);
        background:${type === 'success' ? '#13795b' : type === 'error' ? '#b63b32' : '#016d79'};
      `;
      toast.textContent = message;
      document.body.appendChild(toast);
      setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transition = 'opacity 0.3s';
        setTimeout(() => toast.remove(), 300);
      }, 3000);
    }
    
    // Track if user manually closed a modal
    let userClosedModal = false;
    
    // Batch registration
    async function startBatch() {
      const btn = document.getElementById('start-batch-btn');
      btn.disabled = true;
      btn.textContent = '创建中...';
      
      const body = {
        count: parseInt(document.getElementById('batch-count').value),
        referral_code: document.getElementById('batch-referral').value || undefined,
        username_prefix: document.getElementById('batch-prefix').value || undefined,
        delay: parseFloat(document.getElementById('batch-delay').value),
        delay_max: parseFloat(document.getElementById('batch-delay-max').value),
        max_concurrent: parseInt(document.getElementById('batch-concurrent').value),
        turnstile_sitekey: document.getElementById('batch-turnstile-sitekey')?.value || undefined,
        turnstile_endpoint: document.getElementById('batch-turnstile-endpoint')?.value || undefined,
      };
      
      try {
        const response = await fetch('/api/batch', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body)
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '创建失败');
        showToast('✓ 批量任务已创建: ' + data.batch_job_id);
        refresh();
      } catch (error) {
        showToast('✗ 错误: ' + error.message, 'error');
      } finally {
        btn.disabled = false;
        btn.textContent = '开始批量注册';
      }
    }
    
    const stepLabels = {
      'turnstile_token_acquisition': 'Turnstile 验证',
      'email_creation': '创建临时邮箱',
      'browser_navigation': '打开注册页',
      'form_submission': '提交注册表单',
      'verification_email_retrieval': '获取验证邮件',
      'verification_completion': '完成邮箱验证'
    };

    function stepTimeline(progress) {
      const current = progress?.current_step;
      return `<div class="step-timeline">${(progress?.steps || []).map(step => {
        const isCurrent = step.name === current && progress.status !== 'failed';
        const state = isCurrent ? 'current' : step.status;
        const icon = step.status === 'success' ? '✓' : step.status === 'failed' ? '!' : '';
        const duration = Number.isFinite(step.duration) ? `${step.duration.toFixed(1)} 秒` : '';
        const stateText = isCurrent ? '正在执行' : step.status === 'success' ? '已完成' : step.status === 'failed' ? '失败' : step.status === 'skipped' ? '未执行' : '等待执行';
        return `<div class="timeline-step ${state}">
          <span class="step-dot">${icon}</span>
          <div class="step-name" title="${escapeHtml(step.label || step.name)}">${escapeHtml(step.label || stepLabels[step.name] || step.name)}</div>
          <div class="step-meta">${stateText}${duration ? ` · ${duration}` : ''}</div>
        </div>`;
      }).join('')}</div>`;
    }

    function accountProgressRow(account, open) {
      const progress = account.progress || {steps:[], completed_steps:0, total_steps:6};
      const pct = Math.round(((progress.completed_steps || 0) / Math.max(progress.total_steps || 6, 1)) * 100);
      return `<details class="account-progress" data-account-id="${account.id}" ${open ? 'open' : ''}>
        <summary>
          <div class="account-name"><strong>${escapeHtml(account.username)}</strong><div class="account-email">${escapeHtml(account.email || account.id)}</div></div>
          <div>${statusBadge(account.status)}</div>
          <div class="task-progress"><div class="task-progress-line"><span>${progress.completed_steps || 0} / ${progress.total_steps || 6} 步</span><span>${pct}%</span></div><div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div></div>
          <div class="account-chevron">›</div>
        </summary>
        ${stepTimeline(progress)}
        ${progress.error ? `<div class="step-error"><strong>失败原因：</strong>${escapeHtml(progress.error)}</div>` : ''}
        ${account.status === 'active' && account.password ? `<div class="hint" style="padding:0 6px 18px">邮箱：${escapeHtml(account.email || '-')} · 密码：<code>${escapeHtml(account.password)}</code></div>` : ''}
      </details>`;
    }

    async function viewBatch(batchId) {
      const modal = document.getElementById('account-detail-modal');
      const wasOpen = modal.classList.contains('active');
      if (!wasOpen) userClosedModal = false;
      try {
        const response = await fetch('/api/batch/' + batchId);
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '未知错误');
        const openAccounts = new Set(Array.from(document.querySelectorAll('.account-progress[open]')).map(el => el.dataset.accountId));
        const accountsHtml = data.accounts?.length
          ? data.accounts.map((account, index) => accountProgressRow(account, openAccounts.has(account.id) || (!wasOpen && index === 0))).join('')
          : '<div class="empty-state">无账号</div>';
        const pct = progressPercent(data);
        document.getElementById('detail-modal-title').textContent = '批量任务进度';
        document.getElementById('account-detail-content').innerHTML = `
          <div class="batch-summary">
            <div><div class="summary-label">任务 ID</div><div class="summary-value task-id">${escapeHtml(data.id)}</div></div>
            <div><div class="summary-label">整体进度</div><div class="summary-value">${pct}%</div></div>
            <div><div class="summary-label">已完成</div><div class="summary-value">${data.completed_accounts} / ${data.total_accounts}</div></div>
            <div><div class="summary-label">成功</div><div class="summary-value" style="color:var(--green)">${data.successful_accounts}</div></div>
            <div><div class="summary-label">失败</div><div class="summary-value" style="color:var(--red)">${data.failed_accounts}</div></div>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px"><strong>账号注册明细</strong>${statusBadge(data.status)}</div>
          ${data.error ? `<div class="step-error" style="padding:8px 0 14px">${escapeHtml(data.error)}</div>` : ''}
          <div>${accountsHtml}</div>`;
        modal.classList.add('active');
        if (!userClosedModal && (data.status === 'running' || data.status === 'pending')) {
          setTimeout(() => { if (!userClosedModal) viewBatch(batchId); }, 3000);
        }
      } catch (error) {
        showToast('加载失败: ' + error.message, 'error');
      }
    }
    
    // Account management
    async function loadAccounts() {
      const search = document.getElementById('account-search').value;
      const status = document.getElementById('account-filter').value;
      let url = '/api/accounts?';
      if (status) url += 'status=' + status + '&';
      if (search) url = '/api/accounts/search?q=' + encodeURIComponent(search);
      
      try {
        const response = await fetch(url);
        if (!response.ok) {
          const tbody = document.querySelector('#accounts-table tbody');
          tbody.innerHTML = '<tr><td colspan="7" class="hint">API错误: ' + response.status + '</td></tr>';
          return;
        }
        const data = await response.json();
        const tbody = document.querySelector('#accounts-table tbody');
        if (!data.accounts || !data.accounts.length) {
          tbody.innerHTML = '<tr><td colspan="7" class="hint">暂无账号</td></tr>';
          return;
        }
      tbody.innerHTML = data.accounts.map(a => 
        `<tr>
          <td><input type="checkbox" class="account-checkbox" value="${a.id}"></td>
          <td>${a.id.slice(0,8)}...</td>
          <td>${a.username}</td>
          <td>${a.email || '-'}</td>
          <td>${statusBadge(a.status)}</td>
          <td>${a.registered_at || '-'}</td>
          <td>
            <div class="account-actions">
              <button class="btn btn-secondary" onclick="viewAccount('${a.id}')">详情</button>
              ${a.status !== 'active' ? `<button class="btn" onclick="registerAccount('${a.id}')">注册</button>` : ''}
              <button class="btn btn-danger" onclick="deleteAccount('${a.id}')">删除</button>
            </div>
          </td>
        </tr>`
      ).join('');
      } catch (e) {
        const tbody = document.querySelector('#accounts-table tbody');
        tbody.innerHTML = '<tr><td colspan="7" class="hint">加载失败: ' + e.message + '</td></tr>';
      }
    }
    
    function toggleSelectAll() {
      const checked = document.getElementById('select-all').checked;
      document.querySelectorAll('.account-checkbox').forEach(cb => cb.checked = checked);
    }
    
    async function viewAccount(accountId) {
      userClosedModal = false; // Reset flag when opening modal
      const response = await fetch('/api/accounts/' + accountId);
      const data = await response.json();
      if (!response.ok) {
        showToast('加载失败: ' + (data.detail || '未知错误'), 'error');
        return;
      }
      document.getElementById('detail-modal-title').textContent = '账号详情';
      // Format the detail view with password highlighted for active accounts
      let html = '<div class="batch-summary" style="grid-template-columns:repeat(3,1fr)">';
      html += `<div><div class="summary-label">用户名</div><div class="summary-value">${escapeHtml(data.username)}</div></div>`;
      html += `<div><div class="summary-label">账号状态</div><div class="summary-value">${statusBadge(data.status)}</div></div>`;
      html += `<div><div class="summary-label">注册时间</div><div style="font-size:13px">${formatTime(data.registered_at || data.created_at)}</div></div></div>`;
      html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px 24px">';
      const fieldOrder = ['id', 'username', 'email', 'password', 'status', 'referral_code', 'fullname', 'phone', 'address_line1', 'city', 'state', 'postal_code', 'country', 'registered_at', 'created_at', 'error'];
      for (const key of fieldOrder) {
        if (data[key] !== undefined && data[key] !== null) {
          const isPassword = key === 'password' && data.status === 'active';
          const value = escapeHtml(Array.isArray(data[key]) ? JSON.stringify(data[key]) : (data[key] || '-'));
          html += `<p${isPassword ? ' style="background:var(--green-soft);padding:8px;border-radius:6px"' : ''}><span class="hint">${escapeHtml(key)}</span><br><strong>${value}</strong></p>`;
        }
      }
      html += '</div>';
      
      // Fetch and display real-time registration progress
      try {
        const progressResp = await fetch('/api/accounts/' + accountId + '/progress');
        const progress = await progressResp.json();
        html += '<div style="margin-top:18px;padding-top:16px;border-top:1px solid var(--line)"><strong>注册流程</strong>';
        html += stepTimeline(progress);
        if (progress.error) html += `<div class="step-error"><strong>失败原因：</strong>${escapeHtml(progress.error)}</div>`;
        html += '</div>';
      } catch (e) {
        // Ignore progress fetch errors
      }
      
      document.getElementById('account-detail-content').innerHTML = html;
      document.getElementById('account-detail-modal').classList.add('active');
      
      // Auto-refresh progress if account is still registering (only if user hasn't closed)
      if (!userClosedModal && (data.status === 'registering' || data.status === 'pending')) {
        setTimeout(() => {
          if (!userClosedModal) viewAccount(accountId);
        }, 2000);
      }
    }
    
    async function registerAccount(accountId) {
      if (!confirm('确认注册此账号?')) return;
      const response = await fetch('/api/accounts/' + accountId + '/register', {method: 'POST'});
      const data = await response.json();
      if (response.ok) {
        alert('注册已开始');
        loadAccounts();
      } else {
        alert('错误: ' + (data.detail || '注册失败'));
      }
    }
    
    async function deleteAccount(accountId) {
      if (!confirm('确认删除此账号?')) return;
      const response = await fetch('/api/accounts/' + accountId, {method: 'DELETE'});
      if (response.ok) {
        loadAccounts();
      } else {
        alert('删除失败');
      }
    }
    
    async function bulkDelete() {
      const checked = Array.from(document.querySelectorAll('.account-checkbox:checked')).map(cb => cb.value);
      if (!checked.length) {
        alert('请选择要删除的账号');
        return;
      }
      if (!confirm('确认删除选中的 ' + checked.length + ' 个账号?')) return;
      
      const response = await fetch('/api/accounts/bulk-delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({account_ids: checked})
      });
      const data = await response.json();
      if (response.ok) {
        alert('已删除 ' + data.deleted + ' 个账号');
        loadAccounts();
      }
    }
    
    function showAddAccountModal() {
      document.getElementById('add-account-modal').classList.add('active');
    }
    
    async function createAccount() {
      const body = {
        username: document.getElementById('new-username').value,
        email: document.getElementById('new-email').value,
        password: document.getElementById('new-password').value || undefined,
        referral_code: document.getElementById('new-referral').value || undefined,
      };
      if (!body.username || !body.email) {
        alert('用户名和邮箱必填');
        return;
      }
      
      const response = await fetch('/api/accounts', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
      });
      if (response.ok) {
        closeModal('add-account-modal');
        loadAccounts();
      } else {
        const data = await response.json();
        alert('创建失败: ' + (data.detail || '未知错误'));
      }
    }
    
    async function exportAccounts() {
      const checked = Array.from(document.querySelectorAll('.account-checkbox:checked')).map(cb => cb.value);
      const body = checked.length ? {account_ids: checked} : {};
      
      const response = await fetch('/api/accounts/export', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
      });
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'accounts_export.json';
      a.click();
    }
    
    function closeModal(id) {
      document.getElementById(id).classList.remove('active');
      if (id === 'account-detail-modal') {
        userClosedModal = true;
      }
    }
    
    // Search debounce
    let searchTimeout;
    document.getElementById('account-search').addEventListener('input', () => {
      clearTimeout(searchTimeout);
      searchTimeout = setTimeout(loadAccounts, 300);
    });
    
    // ==================== Domain Functions ====================
    
    async function loadDomainAccounts() {
      // Load active accounts into domain account selector
      try {
        const response = await fetch('/api/accounts?status=active&limit=100');
        const data = await response.json();
        const select = document.getElementById('domain-account');
        select.innerHTML = '<option value="">-- 选择已注册的账号 --</option>';
        if (data.accounts) {
          for (const account of data.accounts) {
            const option = document.createElement('option');
            option.value = account.username;
            option.textContent = `${account.username} (${account.email || '无邮箱'})`;
            option.dataset.password = account.password || '';
            select.appendChild(option);
          }
        }
      } catch (e) {
        console.error('Failed to load accounts:', e);
      }
    }
    
    async function loadDomains() {
      // Load account dropdown
      await loadDomainAccounts();
      
      // Load registered domains table
      try {
        const response = await fetch('/api/domains');
        const data = await response.json();
        const tbody = document.querySelector('#domains-table tbody');
        if (!data.domains || !data.domains.length) {
          tbody.innerHTML = '<tr><td colspan="4" class="hint">暂无域名</td></tr>';
          return;
        }
        tbody.innerHTML = data.domains.map(d => 
          `<tr>
            <td><strong>${d.domain}</strong></td>
            <td>${d.username}</td>
            <td>${d.registered_at || '-'}</td>
            <td style="font-size:12px">${(d.nameservers || []).join('<br>')}</td>
          </tr>`
        ).join('');
      } catch (e) {
        const tbody = document.querySelector('#domains-table tbody');
        tbody.innerHTML = '<tr><td colspan="4" class="hint">加载失败</td></tr>';
      }
    }
    
    async function checkDomain() {
      const accountSelect = document.getElementById('domain-account');
      const username = accountSelect.value;
      const password = accountSelect.options[accountSelect.selectedIndex]?.dataset.password || '';
      const prefix = document.getElementById('domain-prefix').value.trim();
      const suffix = document.getElementById('domain-suffix').value;
      const resultDiv = document.getElementById('domain-result');
      
      if (!username || !prefix) {
        showToast('请选择账号并输入域名前缀', 'error');
        return;
      }
      
      document.getElementById('check-domain-btn').disabled = true;
      resultDiv.innerHTML = '<p class="hint">⏳ 检查中...</p>';
      
      try {
        const response = await fetch('/api/domains/check', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({username, password, domain_prefix: prefix, domain_suffix: suffix})
        });
        const data = await response.json();
        if (data.available) {
          resultDiv.innerHTML = `<p style="color:var(--green)">✓ ${data.domain} 可注册!</p>`;
        } else {
          resultDiv.innerHTML = `<p style="color:var(--amber)">✗ ${data.domain} 不可用 - ${data.message}</p>`;
        }
      } catch (e) {
        resultDiv.innerHTML = `<p style="color:var(--red)">错误: ${e.message}</p>`;
      } finally {
        document.getElementById('check-domain-btn').disabled = false;
      }
    }
    
    async function registerDomain() {
      const accountSelect = document.getElementById('domain-account');
      const username = accountSelect.value;
      const password = accountSelect.options[accountSelect.selectedIndex]?.dataset.password || '';
      const prefix = document.getElementById('domain-prefix').value.trim();
      const suffix = document.getElementById('domain-suffix').value;
      const ns1 = document.getElementById('domain-ns1').value.trim() || 'ns1.cloudflare.com';
      const ns2 = document.getElementById('domain-ns2').value.trim() || 'ns2.cloudflare.com';
      const resultDiv = document.getElementById('domain-result');
      
      if (!username || !prefix) {
        showToast('请选择账号并输入域名前缀', 'error');
        return;
      }
      
      if (!confirm(`确认注册 ${prefix}.${suffix} ?`)) return;
      
      document.getElementById('register-domain-btn').disabled = true;
      resultDiv.innerHTML = '<p class="hint">⏳ 注册中...可能需要 30-60 秒</p>';
      
      try {
        const response = await fetch('/api/domains/register', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            username, password, 
            domain_prefix: prefix, 
            domain_suffix: suffix,
            nameservers: [ns1, ns2]
          })
        });
        const data = await response.json();
        if (data.success) {
          resultDiv.innerHTML = `<p style="color:var(--green)">✓ 注册成功! ${data.domain}</p>`;
          showToast(`✓ 域名注册成功: ${data.domain}`);
          loadDomains();
        } else {
          resultDiv.innerHTML = `<p style="color:var(--red)">注册失败: ${data.error || data.message}</p>`;
          showToast('✗ 注册失败: ' + (data.error || data.message), 'error');
        }
      } catch (e) {
        resultDiv.innerHTML = `<p style="color:var(--red)">错误: ${e.message}</p>`;
        showToast('✗ 错误: ' + e.message, 'error');
      } finally {
        document.getElementById('register-domain-btn').disabled = false;
      }
    }
    
    // Initial load and auto-refresh
    refresh();
    loadAccounts();
    setInterval(refresh, 3000);
  </script>
</body>
</html>"""


DOMAIN_AUTOMATION_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>DigitalPlat 域名自动注册</title>
  <style>
    :root { --ink:#17211d;--muted:#68756f;--line:#dce5e0;--paper:#f3f7f5;--panel:#fff;--green:#087f5b;--green-soft:#e6f5ee;--red:#c2413a;--amber:#a16207;--blue:#2563eb; }
    * { box-sizing:border-box; }
    body { margin:0;background:linear-gradient(180deg,#edf4f0 0,#f8faf9 300px);color:var(--ink);font-family:"Avenir Next","PingFang SC","Noto Sans CJK SC",sans-serif; }
    header { background:#14231d;color:#fff;padding:25px max(24px,calc((100vw - 1420px)/2));display:flex;align-items:center;justify-content:space-between;gap:24px; }
    h1 { margin:0;font-size:25px;letter-spacing:-.02em; }
    .subtitle { color:#afbeb7;font-size:13px;margin-top:5px; }
    .header-actions { display:flex;align-items:center;gap:18px; }
    .back-link { color:#e0ebe6;text-decoration:none;font-size:13px;font-weight:600;padding:8px 12px;border:1px solid #40534a;border-radius:7px;transition:.15s; }
    .back-link:hover { background:#20372d;border-color:#5b7368;transform:translateY(-1px); }
    #connection { color:#b9c8c1;font-size:12px;display:flex;align-items:center;gap:8px; }
    #connection::before { content:"";width:8px;height:8px;border-radius:50%;background:#42d39a;box-shadow:0 0 0 4px rgba(66,211,154,.13); }
    main { max-width:1420px;margin:0 auto;padding:28px 24px 60px; }
    .metrics { display:grid;grid-template-columns:repeat(6,1fr);border-bottom:1px solid var(--line);margin-bottom:28px; }
    .metric { padding:4px 22px 19px;border-right:1px solid var(--line); }
    .metric:first-child { padding-left:0; }.metric:last-child { border:0; }
    .metric-label,.hint { color:var(--muted);font-size:12px;line-height:1.55; }
    .metric-value { font-size:30px;font-weight:650;letter-spacing:-.04em;margin-top:7px;font-variant-numeric:tabular-nums; }
    .layout { display:grid;grid-template-columns:minmax(300px,.8fr) minmax(460px,1.2fr);gap:20px;align-items:start; }
    .stack { display:grid;gap:20px; }
    .panel { background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;box-shadow:0 9px 30px rgba(24,49,38,.04); }
    .panel-head { padding:17px 20px;border-bottom:1px solid var(--line);background:#fbfcfb;display:flex;align-items:center;justify-content:space-between;gap:16px; }
    .panel-head h2 { margin:0;font-size:15px; }.panel-body { padding:20px; }
    label { display:block;color:var(--muted);font-size:12px;margin:0 0 6px; }
    input,select { width:100%;border:1px solid #cbd7d1;border-radius:7px;padding:10px 11px;font:inherit;color:var(--ink);background:#fff; }
    input:focus,select:focus { outline:0;border-color:var(--green);box-shadow:0 0 0 3px rgba(8,127,91,.1); }
    .check-line { display:flex;align-items:flex-start;gap:10px;padding:11px 12px;border:1px solid var(--line);border-radius:8px;background:#f8faf9; }
    .check-line input { width:auto;margin:3px 0 0;accent-color:var(--green); }.check-line label { margin:0;color:var(--ink);font-size:12px; }
    .form-grid { display:grid;grid-template-columns:1fr 1fr;gap:13px; }
    .full { grid-column:1/-1; }.actions { display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-top:16px; }
    button { border:0;border-radius:7px;padding:9px 15px;font:inherit;font-size:13px;font-weight:600;cursor:pointer;transition:.15s; }
    button:hover { transform:translateY(-1px); }button:disabled { opacity:.5;cursor:not-allowed;transform:none; }
    .primary { background:var(--green);color:#fff; }.secondary { background:#fff;color:var(--ink);border:1px solid var(--line); }.danger-link { background:transparent;color:var(--red);padding:4px 6px; }
    .list { display:grid; }.list-row { display:grid;grid-template-columns:minmax(0,1fr) auto;gap:14px;align-items:center;padding:13px 0;border-bottom:1px solid var(--line); }
    .list-row:last-child { border:0;padding-bottom:0; }.list-row:first-child { padding-top:0; }
    .row-title { font-weight:600;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap; }.row-meta { color:var(--muted);font-size:11px;margin-top:4px; }
    .badge { display:inline-block;padding:4px 8px;border-radius:6px;font-size:10px;font-weight:700;letter-spacing:.02em; }
    .badge-valid,.badge-completed,.badge-succeeded,.badge-active,.badge-renewed { color:var(--green);background:var(--green-soft); }.badge-invalid,.badge-failed { color:var(--red);background:#fbe9e7; }.badge-running,.badge-pending { color:var(--blue);background:#eaf0ff; }.badge-untested,.badge-unmanaged,.badge-skipped { color:var(--muted);background:#edf1ef; }
    .token-line { display:flex;align-items:center;gap:8px;flex-wrap:wrap; }.token-mask { font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;color:#415149; }
    table { width:100%;border-collapse:collapse; }th,td { text-align:left;padding:11px 13px;border-bottom:1px solid var(--line);font-size:12px;vertical-align:top; }
    th { color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.04em;background:#f7f9f8; }tbody tr:hover { background:#fbfcfb; }
    .table-wrap { overflow:auto; }.task-id { font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:#425049; }
    .progress { height:7px;border-radius:99px;background:#e5ebe8;overflow:hidden;margin-top:6px; }.progress > span { display:block;height:100%;background:linear-gradient(90deg,#087f5b,#22a477);transition:width .25s; }
    .job { border-top:1px solid var(--line); }.job:first-child { border-top:0; }.job summary { list-style:none;cursor:pointer;padding:16px 20px;display:grid;grid-template-columns:minmax(190px,1.3fr) 110px minmax(180px,1fr) 24px;align-items:center;gap:15px; }
    .job summary::-webkit-details-marker,.attempt summary::-webkit-details-marker { display:none; }.chevron { color:var(--muted);transition:.18s; }.job[open]>.job-summary .chevron,.attempt[open]>.attempt-summary .chevron { transform:rotate(90deg); }
    .job-body { padding:0 20px 18px;background:#fbfcfb;border-top:1px solid var(--line); }.attempt { border-bottom:1px solid var(--line); }.attempt:last-child { border:0; }
    .attempt summary { list-style:none;cursor:pointer;display:grid;grid-template-columns:minmax(190px,1fr) 130px 100px 20px;gap:14px;align-items:center;padding:13px 0; }
    .steps { display:grid;grid-template-columns:repeat(4,1fr);padding:2px 0 17px; }.step { position:relative;padding:29px 8px 0 0;min-width:0; }.step::before { content:"";position:absolute;left:8px;right:-8px;top:12px;height:2px;background:#dce5e0; }.step:last-child::before { right:calc(100% - 8px); }
    .dot { position:absolute;left:0;top:4px;z-index:1;width:18px;height:18px;border-radius:50%;background:#fff;border:2px solid #c7d2cd;display:grid;place-items:center;color:#fff;font-size:10px; }.step.success::before { background:#62b896; }.step.success .dot { background:var(--green);border-color:var(--green); }.step.failed .dot { background:var(--red);border-color:var(--red); }.step.running .dot { border-color:var(--blue);box-shadow:0 0 0 4px rgba(37,99,235,.1);animation:pulse 1.3s infinite; }
    .step-name { font-size:11px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis; }.step-message { font-size:10px;color:var(--muted);margin-top:4px;line-height:1.4;overflow-wrap:anywhere; }
    .empty { text-align:center;color:var(--muted);padding:28px 12px;font-size:12px; }.notice { padding:12px 14px;border-radius:8px;background:#f5f8f6;color:#59675f;font-size:12px;line-height:1.6;margin-bottom:16px; }
    .toast { position:fixed;right:20px;top:20px;z-index:20;padding:12px 16px;color:#fff;border-radius:8px;background:#14231d;box-shadow:0 10px 30px rgba(0,0,0,.18);font-size:13px; }
    @keyframes pulse { 50% { opacity:.45; } }
    @media(max-width:900px){.layout{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,1fr)}.metric{border-bottom:1px solid var(--line)}.job summary{grid-template-columns:1fr auto}.job summary .job-progress{grid-column:1/-1}.attempt summary{grid-template-columns:1fr auto}.attempt summary .attempt-token{grid-column:1/-1}.steps{grid-template-columns:1fr}.step{padding:3px 0 17px 35px}.step::before{left:8px;right:auto;top:12px;bottom:-5px;width:2px;height:auto}.step:last-child::before{display:none}}
    @media(max-width:600px){header{padding:18px 16px;align-items:flex-start}.header-actions{align-items:flex-end;flex-direction:column;gap:8px}.back-link{padding:6px 9px}main{padding:22px 14px 50px}.form-grid{grid-template-columns:1fr}.full{grid-column:auto}.metrics{grid-template-columns:1fr 1fr}.metric{padding:8px 12px 15px}.metric-value{font-size:25px}}
  </style>
</head>
<body>
  <header><div><h1>DigitalPlat 域名自动注册</h1><div class="subtitle">多 Token 调度 · 前缀订阅 · Cloudflare 自动托管 · 注册结果回查</div></div><div class="header-actions"><a class="back-link" href="/">← 返回原控制台</a><div id="connection">正在连接</div></div></header>
  <main>
    <section class="metrics">
      <div class="metric"><div class="metric-label">API Token</div><div class="metric-value" id="stat-tokens">0</div></div>
      <div class="metric"><div class="metric-label">可用 Token</div><div class="metric-value" id="stat-enabled">0</div></div>
      <div class="metric"><div class="metric-label">前缀订阅</div><div class="metric-value" id="stat-subscriptions">0</div></div>
      <div class="metric"><div class="metric-label">运行任务</div><div class="metric-value" id="stat-running">0</div></div>
      <div class="metric"><div class="metric-label">注册成功</div><div class="metric-value" id="stat-domains" style="color:var(--green)">0</div></div>
      <div class="metric"><div class="metric-label">Cloudflare Active</div><div class="metric-value" id="stat-cloudflare" style="color:var(--blue)">0</div></div>
    </section>
    <div class="layout">
      <div class="stack">
        <section class="panel"><div class="panel-head"><h2>API Token 池</h2><span class="hint">仅服务端保存</span></div><div class="panel-body">
          <div class="form-grid"><div><label>Token 名称</label><input id="token-name" placeholder="例如：账号 A"></div><div><label>DigitalPlat API Token</label><input id="token-value" type="password" placeholder="dp_live_..."></div></div>
          <div class="actions"><button class="primary" onclick="addToken()">添加 Token</button><span class="hint">页面不会回显 Token 原文</span></div><div id="token-list" class="list" style="margin-top:18px"></div>
        </div></section>
        <section class="panel"><div class="panel-head"><h2>Cloudflare 托管</h2><span class="hint">Zone 与 NS 自动配置</span></div><div class="panel-body">
          <div class="notice">API Token 建议只授予 <strong>Zone / Zone / Edit</strong> 权限，并限制到目标账户。系统创建 Zone 后读取专属 NS，再通过对应的 DigitalPlat Token 更新委派。</div>
          <div class="form-grid"><div><label>Cloudflare Account ID</label><input id="cf-account-id" placeholder="32 位 Account ID"></div><div><label>Cloudflare API Token</label><input id="cf-token" type="password" placeholder="仅保存于服务端"></div></div>
          <div class="actions"><button class="primary" onclick="saveCloudflare()">保存配置</button><button class="secondary" onclick="testCloudflare()">测试</button><button class="danger-link" onclick="deleteCloudflare()">删除</button></div>
          <div id="cloudflare-state" class="hint" style="margin-top:13px">尚未配置</div>
        </div></section>
        <section class="panel"><div class="panel-head"><h2>自动续期</h2><span class="hint">多 Token 定时检查</span></div><div class="panel-body">
          <div class="notice">复用自动续期仓库的规则：读取每个 Token 的域名，到期前进入续期窗口后调用 DigitalPlat 续期接口。默认每天检查一次、到期前 120 天、免费续期 1 年。</div>
          <div class="form-grid">
            <div><label>到期前多少天续期</label><input id="renew-before" type="number" value="120" min="0" max="3650"></div>
            <div><label>自动检查周期（小时）</label><input id="renew-interval-hours" type="number" value="24" min="1" max="8760"></div>
            <div><label>续期类型</label><select id="renew-type"><option value="free">free（推荐）</option><option value="paid">paid</option></select></div>
            <div><label>续期年数</label><input id="renew-years" type="number" value="1" min="1" max="5"></div>
            <div><label>域名间最小间隔（秒）</label><input id="renew-delay-min" type="number" value="3" min="0" max="3600"></div>
            <div><label>域名间最大间隔（秒）</label><input id="renew-delay-max" type="number" value="6" min="0" max="3600"></div>
            <div class="full check-line"><input id="renew-enabled" type="checkbox" checked><label for="renew-enabled"><strong>启用后台自动续期检查</strong><br><span class="hint">容器运行期间按检查周期执行</span></label></div>
          </div>
          <div class="actions"><button class="primary" onclick="saveRenewal()">保存续期配置</button><button class="secondary" onclick="runRenewal(false)">立即检查并续期</button></div>
          <div id="renewal-state" class="hint" style="margin-top:13px">尚未运行</div>
        </div></section>
        <section class="panel"><div class="panel-head"><h2>前缀订阅</h2><span class="hint">自动生成候选域名</span></div><div class="panel-body">
          <div class="form-grid">
            <div><label>订阅名称</label><input id="sub-name" placeholder="例如：博客域名"></div>
            <div><label>固定前缀</label><input id="sub-prefix" placeholder="例如：blog"></div>
            <div><label>域名后缀</label><input id="sub-suffix" value="us.kg"></div>
            <div><label>容量类型</label><select id="sub-slot"><option value="subscription">subscription</option><option value="paid">paid</option><option value="free">free</option></select></div>
            <div><label>随机字符长度</label><input id="sub-length" type="number" min="2" max="24" value="6"></div>
            <div><label>前缀分隔符</label><select id="sub-separator"><option value="">无分隔符（推荐），例如 bloga1b2c3</option><option value="-">连字符，例如 blog-a1b2c3</option></select><div class="hint">dpdns.org 会强制使用无分隔符</div></div>
            <div class="full check-line"><input id="sub-auto-cloudflare" type="checkbox" onchange="toggleNameservers()"><label for="sub-auto-cloudflare"><strong>注册成功后自动托管到 Cloudflare</strong><br><span class="hint">自动创建 Zone、更新 DigitalPlat NS，并显示激活状态</span></label></div>
            <div class="full" id="manual-nameservers"><label>手动 Nameservers（每行一个）</label><input id="sub-ns1" value="ns1.provider.com" style="margin-bottom:8px"><input id="sub-ns2" value="ns2.provider.com"></div>
          </div>
          <div class="actions"><button class="primary" onclick="addSubscription()">创建订阅</button></div><div id="subscription-list" class="list" style="margin-top:18px"></div>
        </div></section>
      </div>
      <div class="stack">
        <section class="panel"><div class="panel-head"><h2>启动注册任务</h2><span class="hint">Token 自动轮询</span></div><div class="panel-body">
          <div class="notice">DigitalPlat 没有独立的域名可用性查询接口。系统会生成唯一候选名并直接注册；网络结果不明确时先回查该 Token 的域名列表，不会盲目重复提交同一个域名。</div>
          <div class="form-grid"><div><label>前缀订阅</label><select id="job-subscription"><option value="">请先创建订阅</option></select></div><div><label>目标注册数量</label><input id="job-count" type="number" min="1" max="100" value="1"></div><div><label>最大尝试次数</label><input id="job-attempts" type="number" min="1" max="1000" value="10"></div><div><label>Token 范围</label><select id="job-token-mode"><option value="all">使用全部启用 Token</option></select></div><div><label>域名操作最小间隔（秒）</label><input id="job-delay-min" type="number" min="0" max="3600" value="20"></div><div><label>域名操作最大间隔（秒）</label><input id="job-delay-max" type="number" min="0" max="3600" value="45"></div></div>
          <div class="actions"><button class="primary" id="start-job" onclick="startJob()">开始自动注册</button></div>
        </div></section>
        <section class="panel"><div class="panel-head"><h2>注册任务与详细进度</h2><span class="hint">每 3 秒刷新</span></div><div id="job-list"></div></section>
        <section class="panel"><div class="panel-head"><h2>已注册域名</h2><div class="actions" style="margin:0"><span class="hint" id="domain-count">0 个</span><button class="secondary" onclick="syncDomains()">从 DigitalPlat 同步</button><button class="secondary" onclick="hostAllCloudflare()">托管全部未激活域名</button></div></div><div class="table-wrap"><table><thead><tr><th>域名</th><th>Token</th><th>Cloudflare</th><th>续期</th><th>注册时间</th><th>操作</th></tr></thead><tbody id="domain-table"></tbody></table></div></section>
      </div>
    </div>
  </main>
  <script>
    let overview = {tokens:[],subscriptions:[],jobs:[],domains:[],cloudflare:null,renewal:null,stats:{}};
    const esc = value => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
    const badge = status => `<span class="badge badge-${esc(status)}">${esc(({valid:'有效',invalid:'异常',untested:'未测试',running:'运行中',pending:'等待中',completed:'已完成',failed:'失败',succeeded:'成功',active:'已激活',unmanaged:'未托管',renewed:'已续期',skipped:'已跳过'})[status] || status)}</span>`;
    const time = value => value ? new Date(value).toLocaleString() : '-';
    function toast(message, error=false){const el=document.createElement('div');el.className='toast';el.style.background=error?'var(--red)':'#14231d';el.textContent=message;document.body.appendChild(el);setTimeout(()=>el.remove(),3200)}
    async function api(path, options={}){const response=await fetch(path,{...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.detail||data.error||`HTTP ${response.status}`);return data}
    async function refresh(){try{overview=await api('/api/domain-automation');render();document.getElementById('connection').textContent='已更新 '+new Date().toLocaleTimeString()}catch(error){document.getElementById('connection').textContent='服务连接失败'}}
    function render(){const s=overview.stats||{};document.getElementById('stat-tokens').textContent=s.tokens||0;document.getElementById('stat-enabled').textContent=s.enabled_tokens||0;document.getElementById('stat-subscriptions').textContent=s.subscriptions||0;document.getElementById('stat-running').textContent=s.running_jobs||0;document.getElementById('stat-domains').textContent=s.registered_domains||0;document.getElementById('stat-cloudflare').textContent=s.cloudflare_active||0;renderTokens();renderCloudflare();renderRenewal();renderSubscriptions();renderJobs();renderDomains()}
    function renderCloudflare(){const c=overview.cloudflare;const state=document.getElementById('cloudflare-state');if(!c){state.innerHTML='尚未配置';return}document.getElementById('cf-account-id').value=c.account_id||'';state.innerHTML=`${badge(c.last_status)} <span class="token-mask">${esc(c.token_masked)}</span>${c.last_checked_at?' · '+time(c.last_checked_at):''}${c.last_error?' · '+esc(c.last_error):''}`}
    function renderRenewal(){const r=overview.renewal||{};document.getElementById('renew-enabled').checked=r.enabled!==false;document.getElementById('renew-before').value=r.renew_before_days??120;document.getElementById('renew-interval-hours').value=Math.max(1,Math.round((r.interval_seconds||86400)/3600));document.getElementById('renew-type').value=r.renewal_type||'free';document.getElementById('renew-years').value=r.renewal_years||1;document.getElementById('renew-delay-min').value=r.delay_min_seconds??3;document.getElementById('renew-delay-max').value=r.delay_max_seconds??6;const s=r.last_summary||{};document.getElementById('renewal-state').innerHTML=`${badge(r.last_status||'untested')} ${r.last_run_at?'上次运行 '+time(r.last_run_at):'尚未运行'}${r.last_run_at?` · 检查 ${s.checked||0} · 续期 ${s.renewed||0} · 跳过 ${s.skipped||0} · 失败 ${s.failed||0}`:''}${r.last_error?' · '+esc(r.last_error):''}`}
    function renderTokens(){const root=document.getElementById('token-list');root.innerHTML=overview.tokens.length?overview.tokens.map(t=>`<div class="list-row"><div><div class="token-line"><span class="row-title">${esc(t.name)}</span>${badge(t.last_status)}</div><div class="row-meta"><span class="token-mask">${esc(t.token_masked)}</span> · ${t.domain_count==null?'域名数未知':t.domain_count+' 个域名'}${t.last_error?' · '+esc(t.last_error):''}</div></div><div><button class="secondary" onclick="testToken('${t.id}')">测试</button><button class="danger-link" onclick="deleteToken('${t.id}')">删除</button></div></div>`).join(''):'<div class="empty">尚未添加 API Token</div>'}
    function renderSubscriptions(){const root=document.getElementById('subscription-list');root.innerHTML=overview.subscriptions.length?overview.subscriptions.map(s=>{const separator=s.suffix==='dpdns.org'?'':s.separator;const routing=s.auto_cloudflare?'Cloudflare 自动托管':s.nameservers.map(esc).join(' · ');return `<div class="list-row"><div><div class="row-title">${esc(s.name)} ${s.auto_cloudflare?badge('active'):''}</div><div class="row-meta">${esc(s.prefix||'[随机]')}${esc(separator)}${'x'.repeat(Math.min(s.random_length,8))}.${esc(s.suffix)} · ${esc(s.slot_type)}<br>${routing}</div></div><button class="danger-link" onclick="deleteSubscription('${s.id}')">删除</button></div>`}).join(''):'<div class="empty">尚未创建前缀订阅</div>';const select=document.getElementById('job-subscription');const current=select.value;select.innerHTML='<option value="">选择一个前缀订阅</option>'+overview.subscriptions.map(s=>`<option value="${s.id}">${esc(s.name)} — ${esc(s.prefix||'[随机]')}.${esc(s.suffix)}</option>`).join('');if(overview.subscriptions.some(s=>s.id===current))select.value=current}
    function attemptSteps(attempt){const order=['candidate_generation','token_assignment','registration_request','registration_verification'];const byName=Object.fromEntries((attempt.steps||[]).map(s=>[s.name,s]));return `<div class="steps">${order.map(name=>{const step=byName[name]||{label:({'candidate_generation':'生成候选域名','token_assignment':'分配 API Token','registration_request':'提交注册请求','registration_verification':'确认注册结果'})[name],status:'pending',message:'等待执行'};const icon=step.status==='success'?'✓':step.status==='failed'?'!':'';return `<div class="step ${esc(step.status)}"><span class="dot">${icon}</span><div class="step-name">${esc(step.label)}</div><div class="step-message">${esc(step.message)}</div></div>`}).join('')}</div>`}
    function renderJobs(){const root=document.getElementById('job-list');const openJobs=new Set([...document.querySelectorAll('.job[open]')].map(e=>e.dataset.id));const openAttempts=new Set([...document.querySelectorAll('.attempt[open]')].map(e=>e.dataset.id));if(!overview.jobs.length){root.innerHTML='<div class="empty">暂无注册任务</div>';return}root.innerHTML=overview.jobs.map((j,index)=>{const pct=Math.round((j.completed_attempts/Math.max(j.max_attempts,1))*100);const attempts=(j.attempts||[]).map((a,i)=>`<details class="attempt" data-id="${a.id}" ${openAttempts.has(a.id)||(!index&&!i)?'open':''}><summary class="attempt-summary"><div><div class="row-title">${esc(a.domain)}</div><div class="row-meta">${time(a.created_at)}</div></div><div class="attempt-token hint">${esc(a.token_name)}</div><div>${badge(a.status)}</div><div class="chevron">›</div></summary>${attemptSteps(a)}${a.error?`<div class="hint" style="color:var(--red);padding-bottom:14px">${esc(a.error)}</div>`:''}</details>`).join('');return `<details class="job" data-id="${j.id}" ${openJobs.has(j.id)||index===0?'open':''}><summary class="job-summary"><div><div class="row-title">任务 <span class="task-id">${esc(j.id)}</span></div><div class="row-meta">成功 ${j.successful_domains}/${j.target_count} · 失败尝试 ${j.failed_attempts}</div></div><div>${badge(j.status)}</div><div class="job-progress"><div class="hint">尝试 ${j.completed_attempts}/${j.max_attempts}</div><div class="progress"><span style="width:${pct}%"></span></div></div><div class="chevron">›</div></summary><div class="job-body">${j.error?`<div class="hint" style="color:var(--red);padding-top:13px">${esc(j.error)}</div>`:''}${attempts||'<div class="empty">等待生成候选域名</div>'}</div></details>`}).join('')}
    function cloudflareDetail(d){const steps=(d.cloudflare_steps||[]).map(s=>`${esc(s.label)}：${esc(s.message)}`).join('<br>');return `${badge(d.cloudflare_status||'unmanaged')}${steps?`<div class="hint" style="margin-top:5px">${steps}</div>`:''}${d.cloudflare_error?`<div class="hint" style="color:var(--red)">${esc(d.cloudflare_error)}</div>`:''}`}
    function renewalDetail(d){const status=d.renewal_status||'untested';return `${badge(status)}<div class="hint" style="margin-top:5px">到期：${esc(d.expiry_date||'未知')} ${d.renewal_days_remaining==null?'':'· 剩余 '+d.renewal_days_remaining+' 天'}${d.renewed_at?' · 上次续期 '+time(d.renewed_at):''}</div>${d.renewal_error?`<div class="hint" style="color:var(--red)">${esc(d.renewal_error)}</div>`:''}`}
    function renderDomains(){document.getElementById('domain-count').textContent=overview.domains.length+' 个';document.getElementById('domain-table').innerHTML=overview.domains.length?overview.domains.map(d=>`<tr><td><strong>${esc(d.domain)}</strong><div class="hint">${(d.nameservers||[]).map(esc).join(' · ')||'尚未设置 NS'}</div></td><td>${esc(d.token_name)}<div class="hint">${esc(d.slot_type)}</div></td><td>${cloudflareDetail(d)}</td><td>${renewalDetail(d)}</td><td>${time(d.registered_at)}</td><td><button class="secondary" onclick="hostCloudflare('${esc(d.domain)}')">${d.cloudflare_status?'重试/刷新':'托管到 Cloudflare'}</button></td></tr>`).join(''):'<tr><td colspan="6" class="empty">暂无成功注册的域名</td></tr>'}
    async function addToken(){try{await api('/api/domain-automation/tokens',{method:'POST',body:JSON.stringify({name:document.getElementById('token-name').value,token:document.getElementById('token-value').value})});document.getElementById('token-value').value='';toast('Token 已添加');await refresh()}catch(e){toast(e.message,true)}}
    async function testToken(id){try{toast('正在测试 Token');await api(`/api/domain-automation/tokens/${id}/test`,{method:'POST'});await refresh()}catch(e){toast(e.message,true)}}
    async function deleteToken(id){if(!confirm('确认删除这个 API Token？'))return;try{await api(`/api/domain-automation/tokens/${id}`,{method:'DELETE'});await refresh()}catch(e){toast(e.message,true)}}
    function toggleNameservers(){document.getElementById('manual-nameservers').style.display=document.getElementById('sub-auto-cloudflare').checked?'none':'block'}
    async function saveCloudflare(){try{await api('/api/domain-automation/cloudflare',{method:'PUT',body:JSON.stringify({account_id:document.getElementById('cf-account-id').value,api_token:document.getElementById('cf-token').value})});document.getElementById('cf-token').value='';toast('Cloudflare 配置已保存');await refresh()}catch(e){toast(e.message,true)}}
    async function testCloudflare(){try{toast('正在测试 Cloudflare Token');await api('/api/domain-automation/cloudflare/test',{method:'POST'});await refresh()}catch(e){toast(e.message,true)}}
    async function deleteCloudflare(){if(!confirm('确认删除 Cloudflare 配置？'))return;try{await api('/api/domain-automation/cloudflare',{method:'DELETE'});document.getElementById('cf-account-id').value='';toast('Cloudflare 配置已删除');await refresh()}catch(e){toast(e.message,true)}}
    async function saveRenewal(){try{await api('/api/domain-automation/renewal',{method:'PUT',body:JSON.stringify({enabled:document.getElementById('renew-enabled').checked,renew_before_days:Number(document.getElementById('renew-before').value),interval_seconds:Number(document.getElementById('renew-interval-hours').value)*3600,renewal_type:document.getElementById('renew-type').value,renewal_years:Number(document.getElementById('renew-years').value),delay_min_seconds:Number(document.getElementById('renew-delay-min').value),delay_max_seconds:Number(document.getElementById('renew-delay-max').value)})});toast('续期配置已保存');await refresh()}catch(e){toast(e.message,true)}}
    async function runRenewal(force){try{toast('正在检查域名续期');const result=await api('/api/domain-automation/renewal/run',{method:'POST',body:JSON.stringify({force})});toast(`续期检查完成：续期 ${result.renewed}，跳过 ${result.skipped}，失败 ${result.failed}`);await refresh()}catch(e){toast(e.message,true)}}
    async function hostCloudflare(domain){try{toast('正在配置 '+domain);await api(`/api/domain-automation/domains/${encodeURIComponent(domain)}/cloudflare`,{method:'POST'});await refresh()}catch(e){toast(e.message,true)}}
    async function syncDomains(){try{const result=await api('/api/domain-automation/domains/sync',{method:'POST'});toast(`同步完成，新增 ${result.synced} 个域名`);await refresh()}catch(e){toast(e.message,true)}}
    async function hostAllCloudflare(){const domains=overview.domains.filter(d=>d.cloudflare_status!=='active');if(!domains.length){toast('没有待托管域名');return}if(!confirm(`确认依次托管 ${domains.length} 个域名到 Cloudflare？`))return;for(const d of domains){toast('正在配置 '+d.domain);try{await api(`/api/domain-automation/domains/${encodeURIComponent(d.domain)}/cloudflare`,{method:'POST'})}catch(e){toast(`${d.domain}: ${e.message}`,true)}}await refresh()}
    async function addSubscription(){try{const autoCloudflare=document.getElementById('sub-auto-cloudflare').checked;await api('/api/domain-automation/subscriptions',{method:'POST',body:JSON.stringify({name:document.getElementById('sub-name').value,prefix:document.getElementById('sub-prefix').value,suffix:document.getElementById('sub-suffix').value,slot_type:document.getElementById('sub-slot').value,random_length:Number(document.getElementById('sub-length').value),separator:document.getElementById('sub-separator').value,auto_cloudflare:autoCloudflare,nameservers:autoCloudflare?[]:[document.getElementById('sub-ns1').value,document.getElementById('sub-ns2').value]})});toast('前缀订阅已创建');await refresh()}catch(e){toast(e.message,true)}}
    async function deleteSubscription(id){if(!confirm('确认删除这个前缀订阅？'))return;try{await api(`/api/domain-automation/subscriptions/${id}`,{method:'DELETE'});await refresh()}catch(e){toast(e.message,true)}}
    async function startJob(){const button=document.getElementById('start-job');button.disabled=true;try{const job=await api('/api/domain-automation/jobs',{method:'POST',body:JSON.stringify({subscription_id:document.getElementById('job-subscription').value,target_count:Number(document.getElementById('job-count').value),max_attempts:Number(document.getElementById('job-attempts').value),delay_min_seconds:Number(document.getElementById('job-delay-min').value),delay_max_seconds:Number(document.getElementById('job-delay-max').value)})});toast('任务已启动：'+job.id);await refresh()}catch(e){toast(e.message,true)}finally{button.disabled=false}}
    refresh();setInterval(refresh,3000);
  </script>
</body>
</html>"""
