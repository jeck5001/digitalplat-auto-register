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

        def on_step_complete(step: StepResult) -> None:
            job.steps.append({
                "name": step.name,
                "success": step.success,
                "duration": step.duration,
                "message": step.message,
            })
            account.metadata["last_step"] = step.name
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
                account.mark_failed(result.error or "Unknown error", result.error_stage)
                job.error = result.error
                job.status = "failed"
                
        except Exception as e:
            account.mark_failed(str(e))
            job.error = _safe_text(str(e))
            job.status = "failed"
        finally:
            job.finished_at = _timestamp()
            self._active_job_ids.discard(job.id)
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
                batch_job.completed_accounts += 1
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
                    account.metadata['current_step'] = step.name
                    asyncio.create_task(self._account_store.save())

                try:
                    result = await register_with_defaults(
                        username=account.username,
                        password=account.password,
                        referral_code=batch_job.referral_code or DEFAULT_REFERRAL_CODE,
                        phone=_generate_phone_number(),
                        on_step_complete=on_step_complete,
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
                        account.mark_failed(result.error or "Unknown error", result.error_stage)
                        batch_job.failed_accounts += 1
                        
                except Exception as e:
                    account.mark_failed(str(e))
                    batch_job.failed_accounts += 1
                
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
            "max_concurrent": 1
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
                if account.status == AccountStatus.ACTIVE:
                    accounts.append(account.to_dict())
                else:
                    accounts.append(account.to_safe_dict())
        
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
        steps = account.metadata.get('steps', [])
        return {
            "account_id": account_id,
            "username": account.username,
            "status": account.status.value,
            "current_step": account.metadata.get('current_step'),
            "steps": steps,
            "total_steps": len(steps),
            "error": account.error,
            "error_stage": account.error_stage,
        }

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
                account.mark_failed(result.error or "Unknown error", result.error_stage)
                
        except Exception as e:
            account.mark_failed(str(e))
        
        await account_store.save()

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
      --ink:#17201e; --muted:#69726e; --line:#d7ddd8; --paper:#f6f7f3;
      --panel:#ffffff; --green:#13795b; --red:#b63b32; --amber:#a56918;
      --teal:#016d79; --blue:#1a56db;
    }
    * { box-sizing:border-box; }
    body { margin:0; color:var(--ink); background:var(--paper); font-family:"Avenir Next","PingFang SC","Noto Sans CJK SC",sans-serif; }
    main { max-width:1280px; margin:0 auto; padding:24px 20px 48px; }
    header { display:flex; align-items:flex-end; justify-content:space-between; gap:24px; border-bottom:2px solid var(--ink); padding-bottom:18px; flex-wrap:wrap; }
    h1 { margin:0; font-size:28px; font-weight:650; }
    .subtitle { color:var(--muted); font-size:14px; }
    nav { display:flex; gap:8px; margin-top:24px; border-bottom:1px solid var(--line); }
    nav button { border:none; background:none; padding:12px 20px; font:inherit; cursor:pointer; color:var(--muted); border-bottom:3px solid transparent; }
    nav button.active { color:var(--ink); border-bottom-color:var(--green); font-weight:600; }
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
    button.btn { border:0; border-radius:4px; padding:10px 16px; background:var(--green); color:#fff; font:inherit; font-weight:600; cursor:pointer; }
    button.btn:disabled { background:#a7b0ab; cursor:not-allowed; }
    button.btn-secondary { background:var(--panel); color:var(--ink); border:1px solid var(--line); }
    button.btn-danger { background:var(--red); }
    .hint { font-size:12px; color:var(--muted); line-height:1.6; }
    table { width:100%; border-collapse:collapse; }
    th, td { padding:10px 12px; text-align:left; border-bottom:1px solid var(--line); font-size:13px; }
    th { background:var(--paper); font-weight:600; color:var(--muted); }
    tr:hover { background:var(--paper); }
    .badge { padding:3px 8px; border-radius:999px; font-size:11px; font-weight:600; white-space:nowrap; }
    .badge-running { background:#e0f1f1; color:var(--teal); }
    .badge-succeeded, .badge-active { background:#def3e8; color:var(--green); }
    .badge-failed { background:#f8e2df; color:var(--red); }
    .badge-pending { background:#fef3c7; color:var(--amber); }
    .badge-registering { background:#dbeafe; color:var(--blue); }
    .toolbar { display:flex; gap:10px; margin-bottom:16px; align-items:center; flex-wrap:wrap; }
    .search-box { flex:1; min-width:200px; }
    .progress-bar { height:8px; background:var(--line); border-radius:4px; overflow:hidden; margin-top:8px; }
    .progress-fill { height:100%; background:var(--green); transition:width 0.3s; }
    .steps { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
    .step { border:1px solid var(--line); padding:3px 6px; color:var(--muted); font-size:11px; border-radius:3px; }
    .step.ok { border-color:#8dc8ad; color:var(--green); }
    .step.no { border-color:#df9991; color:var(--red); }
    .account-actions { display:flex; gap:6px; }
    .account-actions button { padding:4px 8px; font-size:11px; }
    .modal-overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.5); z-index:100; align-items:center; justify-content:center; }
    .modal-overlay.active { display:flex; }
    .modal { background:#fff; border-radius:8px; padding:24px; max-width:500px; width:90%; max-height:80vh; overflow:auto; }
    .modal h3 { margin-top:0; }
    .modal-actions { display:flex; gap:10px; justify-content:flex-end; margin-top:20px; }
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
    <div class="modal">
      <h3>账号详情</h3>
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
    }
    
    function statusBadge(status) {
      const cls = status === 'active' || status === 'succeeded' ? 'active' :
                  status === 'failed' ? 'failed' :
                  status === 'registering' ? 'running' :
                  status === 'pending' ? 'pending' : 'running';
      return `<span class="badge badge-${cls}">${labels[status] || status}</span>`;
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
    
    function renderRecentAccounts(data) {
      // Load from accounts API
      fetch('/api/accounts?limit=5').then(r => r.json()).then(result => {
        const tbody = document.querySelector('#recent-accounts-table tbody');
        if (!result.accounts.length) {
          tbody.innerHTML = '<tr><td colspan="3" class="hint">暂无账号</td></tr>';
          return;
        }
        tbody.innerHTML = result.accounts.map(a => 
          `<tr><td>${a.username}</td><td>${a.email || '-'}</td><td>${statusBadge(a.status)}</td></tr>`
        ).join('');
      });
    }
    
    function renderBatches(batches) {
      const tbody = document.querySelector('#batch-history-table tbody');
      if (!batches.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="hint">暂无批量任务</td></tr>';
        return;
      }
      tbody.innerHTML = batches.map(b => 
        `<tr>
          <td>${b.id}</td>
          <td>${b.total_accounts}</td>
          <td>${b.completed_accounts}</td>
          <td style="color:var(--green)">${b.successful_accounts}</td>
          <td style="color:var(--red)">${b.failed_accounts}</td>
          <td>${statusBadge(b.status)}</td>
          <td>${b.created_at}</td>
          <td><button class="btn btn-secondary" style="padding:4px 8px;font-size:11px" onclick="viewBatch('${b.id}')">详情</button></td>
        </tr>`
      ).join('');
      
      // Show active batch if any
      const activeBatch = batches.find(b => b.status === 'running');
      const activeSection = document.getElementById('active-batch-section');
      if (activeBatch) {
        activeSection.style.display = 'block';
        const pct = Math.round((activeBatch.completed_accounts / activeBatch.total_accounts) * 100) || 0;
        document.getElementById('active-batch-info').innerHTML = `
          <p><strong>任务ID:</strong> ${activeBatch.id}</p>
          <p><strong>进度:</strong> ${activeBatch.completed_accounts} / ${activeBatch.total_accounts}</p>
          <div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div>
          <p class="hint">成功: ${activeBatch.successful_accounts} | 失败: ${activeBatch.failed_accounts}</p>
        `;
      } else {
        activeSection.style.display = 'none';
      }
    }
    
    function renderJobs(jobs) {
      const tbody = document.querySelector('#history-table tbody');
      if (!jobs.length) {
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
      };
      
      try {
        const response = await fetch('/api/batch', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body)
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '创建失败');
        alert('批量任务已创建: ' + data.batch_job_id);
        refresh();
      } catch (error) {
        alert('错误: ' + error.message);
      } finally {
        btn.disabled = false;
        btn.textContent = '开始批量注册';
      }
    }
    
    const stepLabels = {
      'turnstile_token_acquisition': '🔑 Turnstile',
      'email_creation': '📧 邮箱',
      'browser_navigation': '🌐 导航',
      'form_submission': '📝 表单',
      'verification_email_retrieval': '📬 验证邮件',
      'verification_completion': '✅ 完成'
    };

    async function viewBatch(batchId) {
      const response = await fetch('/api/batch/' + batchId);
      const data = await response.json();
      if (!response.ok) {
        alert('加载失败: ' + (data.detail || '未知错误'));
        return;
      }
      
      // Build account list with progress indicators
      let accountsHtml = '';
      if (data.accounts && data.accounts.length > 0) {
        accountsHtml = '<div style="margin-top:8px">';
        for (const a of data.accounts) {
          const statusColor = a.status === 'active' ? 'green' : (a.status === 'failed' ? 'red' : 'var(--muted)');
          accountsHtml += `<div style="padding:6px 0;border-bottom:1px solid var(--line)">`;
          accountsHtml += `<span style="color:${statusColor}">${a.status === 'active' ? '✓' : (a.status === 'failed' ? '✗' : '⏳')}</span> `;
          accountsHtml += `<strong>${a.username}</strong>`;
          if (a.email) accountsHtml += ` - ${a.email}`;
          if (a.password && a.status === 'active') accountsHtml += ` - <code style="background:#def3e8;padding:2px 6px;border-radius:3px">${a.password}</code>`;
          if (a.error) accountsHtml += `<br><span style="color:var(--red);font-size:12px">${a.error}</span>`;
          accountsHtml += `</div>`;
        }
        accountsHtml += '</div>';
      } else {
        accountsHtml = '<li>无账号</li>';
      }
      
      document.getElementById('account-detail-content').innerHTML = `
        <p><strong>任务ID:</strong> ${data.id}</p>
        <p><strong>状态:</strong> ${data.status}</p>
        <p><strong>总数:</strong> ${data.total_accounts}</p>
        <p><strong>完成:</strong> ${data.completed_accounts}</p>
        <p><strong>成功:</strong> <span style="color:var(--green)">${data.successful_accounts}</span></p>
        <p><strong>失败:</strong> <span style="color:var(--red)">${data.failed_accounts}</span></p>
        <p><strong>创建时间:</strong> ${data.created_at}</p>
        ${data.error ? `<p style="color:var(--red)"><strong>错误:</strong> ${data.error}</p>` : ''}
        <details style="margin-top:12px" open>
          <summary>账号列表 (${data.accounts?.length || 0})</summary>
          ${accountsHtml}
        </details>
      `;
      document.getElementById('account-detail-modal').classList.add('active');
      
      // Auto-refresh if batch is still running
      if (data.status === 'running' || data.status === 'pending') {
        setTimeout(() => viewBatch(batchId), 3000);
      }
    }
    
    // Account management
    async function loadAccounts() {
      const search = document.getElementById('account-search').value;
      const status = document.getElementById('account-filter').value;
      let url = '/api/accounts?';
      if (status) url += 'status=' + status + '&';
      if (search) url = '/api/accounts/search?q=' + encodeURIComponent(search);
      
      const response = await fetch(url);
      const data = await response.json();
      const tbody = document.querySelector('#accounts-table tbody');
      if (!data.accounts.length) {
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
    }
    
    function toggleSelectAll() {
      const checked = document.getElementById('select-all').checked;
      document.querySelectorAll('.account-checkbox').forEach(cb => cb.checked = checked);
    }
    
    async function viewAccount(accountId) {
      const response = await fetch('/api/accounts/' + accountId);
      const data = await response.json();
      if (!response.ok) {
        alert('加载失败: ' + (data.detail || '未知错误'));
        return;
      }
      // Format the detail view with password highlighted for active accounts
      let html = '';
      const fieldOrder = ['id', 'username', 'email', 'password', 'status', 'referral_code', 'fullname', 'phone', 'address_line1', 'city', 'state', 'postal_code', 'country', 'registered_at', 'created_at', 'error'];
      for (const key of fieldOrder) {
        if (data[key] !== undefined && data[key] !== null) {
          const isPassword = key === 'password' && data.status === 'active';
          const value = Array.isArray(data[key]) ? JSON.stringify(data[key]) : (data[key] || '-');
          html += `<p${isPassword ? ' style="background:#def3e8;padding:4px 8px;border-radius:4px"' : ''}><strong>${key}:</strong> ${value}</p>`;
        }
      }
      
      // Fetch and display real-time registration progress
      try {
        const progressResp = await fetch('/api/accounts/' + accountId + '/progress');
        const progress = await progressResp.json();
        if (progress.steps && progress.steps.length > 0) {
          html += '<div style="margin-top:16px;padding-top:12px;border-top:1px solid var(--line)">';
          html += '<strong>注册进度:</strong>';
          html += '<div class="steps" style="margin-top:8px">';
          const stepLabels = {
            'turnstile_token_acquisition': '🔑 Turnstile验证',
            'email_creation': '📧 创建邮箱',
            'browser_navigation': '🌐 浏览器导航',
            'form_submission': '📝 提交表单',
            'verification_email_retrieval': '📬 获取验证邮件',
            'verification_completion': '✅ 完成验证'
          };
          for (const step of progress.steps) {
            const label = stepLabels[step.name] || step.name;
            const cls = step.success === true ? 'ok' : (step.success === false ? 'no' : '');
            const status = step.success === true ? '✓' : (step.success === false ? '✗' : '⏳');
            html += `<span class="step ${cls}" title="${step.message || ''}">${status} ${label}${step.duration ? ' (' + step.duration.toFixed(1) + 's)' : ''}</span>`;
          }
          html += '</div>';
          if (progress.current_step) {
            const currentLabel = stepLabels[progress.current_step] || progress.current_step;
            html += `<p class="hint" style="margin-top:8px">当前步骤: ${currentLabel}</p>`;
          }
          if (progress.error) {
            html += `<p style="color:var(--red);margin-top:8px"><strong>错误:</strong> ${progress.error}</p>`;
          }
          html += '</div>';
        }
      } catch (e) {
        // Ignore progress fetch errors
      }
      
      document.getElementById('account-detail-content').innerHTML = html;
      document.getElementById('account-detail-modal').classList.add('active');
      
      // Auto-refresh progress if account is still registering
      if (data.status === 'registering' || data.status === 'pending') {
        setTimeout(() => viewAccount(accountId), 2000);
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
    }
    
    // Search debounce
    let searchTimeout;
    document.getElementById('account-search').addEventListener('input', () => {
      clearTimeout(searchTimeout);
      searchTimeout = setTimeout(loadAccounts, 300);
    });
    
    // Initial load and auto-refresh
    refresh();
    loadAccounts();
    setInterval(refresh, 3000);
  </script>
</body>
</html>"""
