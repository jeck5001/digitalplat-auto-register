"""Web management console for batch DigitalPlat registrations and account management."""

import argparse
import asyncio
import json
import logging
import os
import secrets
import tempfile
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

logger = logging.getLogger(__name__)

from .core.account import (
    Account, AccountStatus, AccountStore, BatchRegistrationJob
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
        max_concurrent: int = 1,
        turnstile_sitekey: Optional[str] = None,
        turnstile_endpoint: Optional[str] = None,
    ) -> BatchRegistrationJob:
        """Start a batch registration job creating multiple accounts."""
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
                max_concurrent=min(max_concurrent, MAX_CONCURRENT_REGISTRATIONS ),
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

        async def register_one(account_id: str) -> None:
            async with semaphore:
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
            # Process accounts with concurrency control
            tasks = []
            for account_id in batch_job.account_ids:
                tasks.append(asyncio.create_task(register_one(account_id)))
                # Add delay between starting registrations
                if batch_job.delay_between_registrations > 0:
                    await asyncio.sleep(batch_job.delay_between_registrations)
            
            await asyncio.gather(*tasks, return_exceptions=True)
            
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
) -> FastAPI:
    """Create FastAPI application with all routes."""
    
    if account_store is None:
        account_store = AccountStore()
    if manager is None:
        manager = RegistrationManager(account_store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await account_store.load()
        await manager.load()
        app.state.registration_manager = manager
        app.state.account_store = account_store
        yield

    app = FastAPI(
        title="DigitalPlat Register Console",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    # ==================== Dashboard ====================

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        return DASHBOARD_HTML

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        return {
            "ok": True,
            "active_jobs": list(manager._active_job_ids),
            "batch_jobs": len([j for j in account_store.get_all_batch_jobs() if j.status == "running"]),
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
        
        batch_job = await manager.start_batch(
            count=count,
            referral_code=request.get("referral_code", DEFAULT_REFERRAL_CODE),
            username_prefix=request.get("username_prefix"),
            delay=request.get("delay", 5.0),
            max_concurrent=request.get("max_concurrent", 1),
            turnstile_sitekey=request.get("turnstile_sitekey"),
            turnstile_endpoint=request.get("turnstile_endpoint"),
        )
        
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

    # ==================== Domain Registration ====================

    @app.get("/api/domains")
    async def list_domains() -> Dict[str, Any]:
        """List registered domains from all active accounts."""
        accounts = account_store.get_accounts_by_status(AccountStatus.ACTIVE)
        domains = []
        for account in accounts:
            account_domains = account.metadata.get("domains", [])
            for domain in account_domains:
                domains.append({
                    "username": account.username,
                    "domain": domain.get("domain"),
                    "registered_at": domain.get("registered_at"),
                    "nameservers": domain.get("nameservers", []),
                })
        return {"total": len(domains), "domains": domains}

    @app.post("/api/domains/register")
    async def register_domain(request: Dict[str, Any]) -> Dict[str, Any]:
        """Register a new domain using DigitalPlat account credentials."""
        from .services.domain_registrar import (
            DomainRegistrar, DomainRegistrationConfig, register_domain_with_defaults
        )
        
        username = request.get("username")
        password = request.get("password")
        domain_prefix = request.get("domain_prefix")
        domain_suffix = request.get("domain_suffix", "dpdns.org")
        nameservers = request.get("nameservers")
        proxy = request.get("proxy")
        
        if not username or not domain_prefix:
            raise HTTPException(
                status_code=400, 
                detail="username and domain_prefix are required"
            )
        
        # Auto-lookup password from AccountStore if not provided
        if not password:
            accounts = account_store.get_accounts_by_status(AccountStatus.ACTIVE)
            for account in accounts:
                if account.username == username:
                    password = account.password
                    break
            if not password:
                raise HTTPException(
                    status_code=404,
                    detail=f"Account '{username}' not found or has no saved password"
                )
        
        # Run registration asynchronously - return task ID immediately
        result = await register_domain_with_defaults(
            username=username,
            password=password,
            domain_prefix=domain_prefix,
            domain_suffix=domain_suffix,
            nameservers=nameservers,
            proxy=proxy,
        )
        
        # If successful, store domain info in account metadata
        if result.success:
            accounts = account_store.get_accounts_by_status(AccountStatus.ACTIVE)
            for account in accounts:
                if account.username == username:
                    if "domains" not in account.metadata:
                        account.metadata["domains"] = []
                    account.metadata["domains"].append({
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
        """Check domain availability without registering."""
        from .services.domain_registrar import DomainRegistrar, DomainRegistrationConfig
        
        username = request.get("username")
        password = request.get("password")
        domain_prefix = request.get("domain_prefix")
        domain_suffix = request.get("domain_suffix", "dpdns.org")
        
        if not username or not domain_prefix:
            raise HTTPException(
                status_code=400, 
                detail="username and domain_prefix are required"
            )
        
        # Auto-lookup password from AccountStore if not provided
        if not password:
            accounts = account_store.get_accounts_by_status(AccountStatus.ACTIVE)
            for account in accounts:
                if account.username == username:
                    password = account.password
                    break
            if not password:
                raise HTTPException(
                    status_code=404,
                    detail=f"Account '{username}' not found or has no saved password"
                )
        
        config = DomainRegistrationConfig(
            username=username,
            password=password,
            domain_prefix=domain_prefix,
            domain_suffix=domain_suffix,
        )
        
        registrar = DomainRegistrar(config)
        await registrar._init_browser(headless=True)
        
        try:
            login_result = await registrar.login()
            if not login_result.success:
                return {
                    "available": False,
                    "domain": f"{domain_prefix}.{domain_suffix}",
                    "message": f"Login failed: {login_result.error}"
                }
            
            check_result = await registrar.check_domain_availability()
            return {
                "available": check_result.available,
                "domain": check_result.domain,
                "message": check_result.message,
            }
        finally:
            await registrar._close_browser()

    return app


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
      <button onclick="showTab('domains', this)">🌐 域名注册</button>
      <button onclick="showTab('history', this)">📜 任务记录</button>
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
                <label>注册间隔（秒）</label>
                <input type="number" id="batch-delay" value="5" min="0" max="60">
              </div>
              <div>
                <label>并发数</label>
                <select id="batch-concurrent">
                  <option value="1">1 (推荐)</option>
                  <option value="2">2</option>
                  <option value="3">3</option>
                </select>
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
        <div class="card-header">注册免费域名 (DigitalPlat)</div>
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
