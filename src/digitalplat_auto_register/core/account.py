"""
Account data model and storage manager for DigitalPlat Auto Register.

This module provides persistent storage and management for registered accounts,
including CRUD operations and batch registration support.
"""

import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from loguru import logger


class AccountStatus(str, Enum):
    """Account registration status"""
    PENDING = "pending"
    REGISTERING = "registering"
    ACTIVE = "active"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclass
class Account:
    """Represents a registered DigitalPlat account."""
    
    # Account credentials
    username: str
    email: str
    password: str
    
    # Profile details
    fullname: Optional[str] = None
    phone: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = "US"
    
    # Registration metadata
    id: str = field(default_factory=lambda: uuid4().hex[:16])
    referral_code: Optional[str] = None
    status: AccountStatus = AccountStatus.PENDING
    email_verified: bool = False
    account_created: bool = False
    
    # Timestamps
    created_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())
    registered_at: Optional[str] = None
    
    # Error tracking
    error: Optional[str] = None
    error_stage: Optional[str] = None
    retry_count: int = 0
    
    # Additional metadata
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert account to dictionary."""
        result = asdict(self)
        result['status'] = self.status.value
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Account':
        """Create Account from dictionary."""
        # Handle status enum
        if 'status' in data and isinstance(data['status'], str):
            data['status'] = AccountStatus(data['status'])
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
    
    def mark_registering(self) -> 'Account':
        """Mark account as being registered."""
        self.status = AccountStatus.REGISTERING
        self.updated_at = datetime.now().astimezone().isoformat()
        return self
    
    def mark_active(self) -> 'Account':
        """Mark account as active/registered successfully."""
        self.status = AccountStatus.ACTIVE
        self.email_verified = True
        self.account_created = True
        self.registered_at = datetime.now().astimezone().isoformat()
        self.updated_at = datetime.now().astimezone().isoformat()
        self.error = None
        self.error_stage = None
        return self
    
    def mark_failed(self, error: str, stage: Optional[str] = None) -> 'Account':
        """Mark account as failed."""
        self.status = AccountStatus.FAILED
        self.error = error
        self.error_stage = stage
        self.updated_at = datetime.now().astimezone().isoformat()
        return self
    
    def to_safe_dict(self) -> Dict[str, Any]:
        """Return account info with sensitive data redacted for logging/display."""
        data = self.to_dict()
        # Mask password
        data['password'] = '***' if self.password else None
        return data


@dataclass
class BatchRegistrationJob:
    """Represents a batch registration job containing multiple accounts."""
    
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    referral_code: Optional[str] = None
    status: str = "pending"  # pending, running, completed, failed, paused
    created_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    
    # Progress tracking
    total_accounts: int = 0
    completed_accounts: int = 0
    successful_accounts: int = 0
    failed_accounts: int = 0
    
    # Account references
    account_ids: List[str] = field(default_factory=list)
    
    # Settings
    delay_between_registrations: float = 5.0
    delay_min_seconds: float = 5.0
    delay_max_seconds: float = 10.0
    max_concurrent: int = 1
    username_prefix: Optional[str] = None
    
    # Error tracking
    error: Optional[str] = None
    
    # Additional metadata (e.g., turnstile config)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'BatchRegistrationJob':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class AccountStore:
    """Persistent storage for accounts and batch registration jobs."""
    
    DEFAULT_ACCOUNTS_PATH = "/app/data/accounts.json"
    
    def __init__(self, data_path: Optional[Path] = None) -> None:
        self._data_path = Path(data_path or os.getenv("ACCOUNTS_PATH", self.DEFAULT_ACCOUNTS_PATH))
        self._accounts: Dict[str, Account] = {}
        self._batch_jobs: Dict[str, BatchRegistrationJob] = {}
        self._loaded = False
    
    def set_pool(self, pool) -> None:
        """Set AccountPool reference for syncing"""
        self._pool = pool
        if self._loaded:
            self._sync_all_to_pool()
    
    def set_stats(self, stats) -> None:
        """Set StatisticsCollector reference for recording"""
        self._stats = stats
    
    @property
    def pool(self):
        return getattr(self, '_pool', None)
    
    @property
    def statistics(self):
        return getattr(self, '_stats', None)
    
    async def load(self) -> None:
        """Load accounts and jobs from disk, cleaning up interrupted operations."""
        import asyncio
        if self._loaded:
            return
        
        if self._data_path.exists():
            try:
                payload = json.loads(self._data_path.read_text(encoding="utf-8"))
                
                # Load accounts
                raw_accounts = payload.get("accounts", [])
                for raw in raw_accounts:
                    try:
                        account = Account.from_dict(raw)
                        # Reset "registering" accounts that were interrupted on restart
                        if account.status == AccountStatus.REGISTERING:
                            account.status = AccountStatus.FAILED
                            account.error = "Registration interrupted by service restart"
                        self._accounts[account.id] = account
                    except Exception as e:
                        logger.warning(f"Failed to load account: {e}")
                
                # Load batch jobs
                raw_jobs = payload.get("batch_jobs", [])
                for raw in raw_jobs:
                    try:
                        job = BatchRegistrationJob.from_dict(raw)
                        # Clean up batch jobs that were interrupted
                        if job.status == "running":
                            job.status = "failed"
                            job.error = "Batch job interrupted by service restart"
                        self._batch_jobs[job.id] = job
                    except Exception as e:
                        logger.warning(f"Failed to load batch job: {e}")
                
                logger.info(f"Loaded {len(self._accounts)} accounts, {len(self._batch_jobs)} batch jobs")
            except (OSError, ValueError) as e:
                logger.error(f"Failed to load data: {e}")
        
        self._loaded = True
        self._sync_all_to_pool()
    
    def _persist(self) -> None:
        """Save data to disk atomically."""
        self._data_path.parent.mkdir(parents=True, exist_ok=True)
        
        payload = {
            "version": 1,
            "accounts": [account.to_dict() for account in self._accounts.values()],
            "batch_jobs": [job.to_dict() for job in self._batch_jobs.values()],
            "saved_at": datetime.now().astimezone().isoformat()
        }
        
        fd, temp_path = tempfile.mkstemp(
            prefix=".accounts-",
            suffix=".json",
            dir=str(self._data_path.parent)
        )
        
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=True, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self._data_path)
        except Exception:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise
    
    async def save(self) -> None:
        """Async wrapper for _persist."""
        import asyncio
        await asyncio.get_event_loop().run_in_executor(None, self._persist)
    
    # Account CRUD operations
    
    def create_account(self, account: Account) -> Account:
        """Add a new account to the store."""
        self._accounts[account.id] = account
        self._sync_to_pool(account)
        return account
    
    def _account_to_profile(self, account: 'Account'):
        """Convert Account to UserProfile for pool sync"""
        from ..types import UserProfile
        return UserProfile(
            username=account.username,
            email=account.email,
            password=account.password,
            fullname=account.fullname or account.username,
            phone=account.phone or "",
            referral_code=account.referral_code or "",
        )

    @staticmethod
    def _pool_status(account: Account):
        from .account_pool import AccountStatus as PoolAccountStatus

        return {
            AccountStatus.ACTIVE: PoolAccountStatus.ACTIVE,
            AccountStatus.PENDING: PoolAccountStatus.IN_USE,
            AccountStatus.REGISTERING: PoolAccountStatus.IN_USE,
            AccountStatus.FAILED: PoolAccountStatus.SUSPENDED,
            AccountStatus.EXPIRED: PoolAccountStatus.SUSPENDED,
        }[account.status]

    def _sync_to_pool(self, account: Account) -> None:
        pool = self.pool
        if not pool:
            return
        try:
            profile = self._account_to_profile(account)
            metadata = dict(account.metadata)
            metadata["legacy_status"] = account.status.value
            if hasattr(pool, "sync_account"):
                pool.sync_account(
                    profile,
                    account_id=account.id,
                    status=self._pool_status(account),
                    metadata=metadata,
                )
            else:
                pool.add_account(profile, account_id=account.id, tags=["legacy_sync"], metadata=metadata)
        except Exception as error:
            logger.warning(f"Failed to sync account {account.id} to account pool: {error}")

    def _sync_all_to_pool(self) -> None:
        pool = self.pool
        if pool and hasattr(pool, "delete_legacy_accounts_except"):
            pool.delete_legacy_accounts_except(list(self._accounts))
        for account in self._accounts.values():
            self._sync_to_pool(account)
    
    def get_account(self, account_id: str) -> Optional[Account]:
        """Get account by ID."""
        return self._accounts.get(account_id)
    
    def get_all_accounts(self) -> List[Account]:
        """Get all accounts."""
        return list(self._accounts.values())
    
    def get_accounts_by_status(self, status: AccountStatus) -> List[Account]:
        """Get accounts filtered by status."""
        return [a for a in self._accounts.values() if a.status == status]
    
    def update_account(self, account_id: str, **kwargs) -> Optional[Account]:
        """Update account fields."""
        account = self._accounts.get(account_id)
        if not account:
            return None
        
        for key, value in kwargs.items():
            if hasattr(account, key):
                if key == "status" and isinstance(value, str):
                    value = AccountStatus(value)
                setattr(account, key, value)

        account.updated_at = datetime.now().astimezone().isoformat()
        self._sync_to_pool(account)
        return account
    
    def delete_account(self, account_id: str) -> bool:
        """Delete an account by ID."""
        if account_id in self._accounts:
            del self._accounts[account_id]
            if self.pool:
                try:
                    self.pool.delete_account(account_id)
                except Exception as error:
                    logger.warning(f"Failed to delete account {account_id} from account pool: {error}")
            return True
        return False
    
    def delete_accounts(self, account_ids: List[str]) -> int:
        """Delete multiple accounts, returns count of deleted."""
        deleted = 0
        for aid in account_ids:
            if self.delete_account(aid):
                deleted += 1
        return deleted
    
    def count_accounts(self) -> Dict[str, int]:
        """Get count of accounts by status."""
        counts = {status.value: 0 for status in AccountStatus}
        for account in self._accounts.values():
            counts[account.status.value] += 1
        counts["total"] = len(self._accounts)
        return counts
    
    # Batch job operations
    
    def create_batch_job(self, job: BatchRegistrationJob) -> BatchRegistrationJob:
        """Create a new batch registration job."""
        self._batch_jobs[job.id] = job
        return job
    
    def get_batch_job(self, job_id: str) -> Optional[BatchRegistrationJob]:
        """Get batch job by ID."""
        return self._batch_jobs.get(job_id)
    
    def get_all_batch_jobs(self) -> List[BatchRegistrationJob]:
        """Get all batch jobs, newest first."""
        return sorted(
            self._batch_jobs.values(),
            key=lambda j: j.created_at,
            reverse=True
        )
    
    def update_batch_job(self, job_id: str, **kwargs) -> Optional[BatchRegistrationJob]:
        """Update batch job fields."""
        job = self._batch_jobs.get(job_id)
        if not job:
            return None
        
        for key, value in kwargs.items():
            if hasattr(job, key):
                setattr(job, key, value)
        
        return job
    
    def delete_batch_job(self, job_id: str) -> bool:
        """Delete a batch job."""
        if job_id in self._batch_jobs:
            del self._batch_jobs[job_id]
            return True
        return False
    
    # Search and filter
    
    def search_accounts(self, query: str) -> List[Account]:
        """Search accounts by username, email, or fullname."""
        query_lower = query.lower()
        results = []
        for account in self._accounts.values():
            if (query_lower in account.username.lower() or
                query_lower in account.email.lower() or
                (account.fullname and query_lower in account.fullname.lower())):
                results.append(account)
        return results
    
    def get_overview(self) -> Dict[str, Any]:
        """Get overview statistics."""
        return {
            "accounts": self.count_accounts(),
            "active_batch_jobs": sum(
                1 for j in self._batch_jobs.values() if j.status == "running"
            ),
            "total_batch_jobs": len(self._batch_jobs)
        }
