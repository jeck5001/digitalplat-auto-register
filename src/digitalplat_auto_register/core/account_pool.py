"""
Account Pool Management for DigitalPlat Auto Register

This module provides account pool management functionality including:
- Account storage and persistence (SQLite)
- Account health status tracking
- Account selection strategies (random, round-robin, least-recently-used)
- Account lifecycle management (creation, activation, suspension, retirement)
"""

import json
import os
import sqlite3
import secrets
import string
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict, field

from loguru import logger

from ..types import UserProfile


class AccountStatus(str, Enum):
    """Account lifecycle status"""
    ACTIVE = "active"           # Ready to use
    IN_USE = "in_use"           # Currently being used
    COOLING_DOWN = "cooling_down"  # Temporary cooldown after use
    SUSPENDED = "suspended"     # Suspended due to failures
    BANNED = "banned"           # Permanently banned
    PASSWORD_EXPIRED = "password_expired"  # Password needs reset


class SelectionStrategy(str, Enum):
    """Account selection strategy"""
    RANDOM = "random"
    ROUND_ROBIN = "round_robin"
    LEAST_RECENTLY_USED = "least_recently_used"
    MOST_SUCCESS_RATE = "most_success_rate"
    WEIGHTED = "weighted"


@dataclass
class AccountMetrics:
    """Account usage metrics"""
    total_uses: int = 0
    successful_uses: int = 0
    failed_uses: int = 0
    total_successes: int = 0  # Total successful registrations
    total_failures: int = 0   # Total failed registrations
    last_used_at: Optional[str] = None
    last_success_at: Optional[str] = None
    last_failure_at: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    
    @property
    def success_rate(self) -> float:
        """Calculate success rate"""
        if self.total_uses == 0:
            return 1.0
        return self.successful_uses / self.total_uses
    
    @property
    def failure_rate(self) -> float:
        """Calculate failure rate"""
        if self.total_uses == 0:
            return 0.0
        return self.failed_uses / self.total_uses


@dataclass
class AccountEntry:
    """Represents an account in the pool"""
    id: str
    profile: Dict[str, Any]  # UserProfile as dict
    status: str = AccountStatus.ACTIVE.value
    metrics: AccountMetrics = field(default_factory=AccountMetrics)
    tags: List[str] = field(default_factory=list)
    notes: str = ""
    cooldown_until: Optional[str] = None
    domain_registered: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @classmethod
    def from_profile(cls, profile: UserProfile, account_id: Optional[str] = None, **kwargs) -> "AccountEntry":
        """Create AccountEntry from UserProfile"""
        return cls(
            id=account_id or cls._generate_id(),
            profile={
                "username": profile.username,
                "email": profile.email,
                "fullname": profile.fullname,
                "phone": profile.phone,
                "password": profile.password,
                "address_line1": profile.address_line1,
                "address_line2": profile.address_line2,
                "city": profile.city,
                "state": profile.state,
                "postal_code": profile.postal_code,
                "country": profile.country,
                "referral_code": profile.referral_code,
            },
            **kwargs,
        )
    
    @staticmethod
    def _generate_id() -> str:
        """Generate a unique account ID"""
        timestamp = int(time.time() * 1000)
        random_part = secrets.token_hex(4)
        return f"acc_{timestamp}_{random_part}"
    
    def to_profile(self) -> UserProfile:
        """Convert to UserProfile"""
        return UserProfile(**self.profile)
    
    def is_available(self) -> bool:
        """Check if account is available for use"""
        if self.status != AccountStatus.ACTIVE.value:
            return False
        if self.cooldown_until:
            cooldown_time = datetime.fromisoformat(self.cooldown_until)
            if datetime.now() < cooldown_time:
                return False
        return True
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AccountEntry":
        """Create from dictionary"""
        data["metrics"] = AccountMetrics(**data.get("metrics", {}))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class AccountPool:
    """
    Account pool manager with SQLite persistence
    
    Features:
    - Persistent account storage
    - Health monitoring and metrics
    - Multiple selection strategies
    - Tag-based filtering
    - Batch operations
    """
    
    DEFAULT_DB_PATH = "account_pool.db"
    
    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize account pool
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path or self.DEFAULT_DB_PATH
        self._round_robin_index = 0
        self._init_database()
    
    def _init_database(self) -> None:
        """Initialize SQLite database and create tables if not exists"""
        try:
            # Ensure parent directory exists
            db_dir = os.path.dirname(self.db_path)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
            
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS accounts (
                        id TEXT PRIMARY KEY,
                        profile TEXT NOT NULL,
                        status TEXT DEFAULT 'active',
                        metrics TEXT DEFAULT '{}',
                        tags TEXT DEFAULT '[]',
                        notes TEXT DEFAULT '',
                        cooldown_until TEXT,
                        domain_registered TEXT DEFAULT '[]',
                        metadata TEXT DEFAULT '{}',
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_accounts_status 
                    ON accounts(status)
                """)
                
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS account_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        account_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        event_data TEXT,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (account_id) REFERENCES accounts(id)
                    )
                """)
                
                conn.commit()
            logger.debug(f"Account pool database initialized at {self.db_path}")
        except sqlite3.Error as e:
            logger.error(f"Failed to initialize account pool database: {e}")
            raise
    
    def add_account(
        self,
        profile: UserProfile,
        account_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        notes: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AccountEntry:
        """
        Add a new account to the pool
        
        Args:
            profile: User profile data
            account_id: Optional custom account ID
            tags: Optional tags for categorization
            notes: Optional notes
            metadata: Additional metadata
            
        Returns:
            Created AccountEntry
        """
        entry = AccountEntry.from_profile(
            profile,
            account_id=account_id,
            tags=tags or [],
            notes=notes,
            metadata=metadata or {},
        )
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO accounts (id, profile, status, metrics, tags, notes, domain_registered, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry.id,
                        json.dumps(entry.profile),
                        entry.status,
                        json.dumps(asdict(entry.metrics)),
                        json.dumps(entry.tags),
                        entry.notes,
                        json.dumps(entry.domain_registered),
                        json.dumps(entry.metadata),
                    ),
                )
                conn.commit()
            
            logger.info(f"Account added to pool: {entry.id} ({profile.email})")
            self._log_event(entry.id, "account_added", {"email": profile.email})
            return entry
            
        except sqlite3.IntegrityError:
            logger.error(f"Account with ID {account_id} already exists")
            raise ValueError(f"Account with ID {account_id} already exists")
        except sqlite3.Error as e:
            logger.error(f"Failed to add account: {e}")
            raise
    
    def add_accounts_bulk(self, profiles: List[UserProfile], tags: Optional[List[str]] = None) -> List[AccountEntry]:
        """
        Add multiple accounts to the pool
        
        Args:
            profiles: List of user profiles
            tags: Optional tags for all accounts
            
        Returns:
            List of created AccountEntry objects
        """
        entries = []
        for profile in profiles:
            try:
                entry = self.add_account(profile, tags=tags)
                entries.append(entry)
            except ValueError:
                logger.warning(f"Skipping duplicate account: {profile.email}")
        
        logger.info(f"Bulk added {len(entries)} accounts to pool")
        return entries
    
    def get_account(self, account_id: str) -> Optional[AccountEntry]:
        """
        Get account by ID
        
        Args:
            account_id: Account ID
            
        Returns:
            AccountEntry if found, None otherwise
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT * FROM accounts WHERE id = ?",
                    (account_id,),
                )
                row = cursor.fetchone()
                
                if row:
                    return AccountEntry.from_dict({
                        "id": row["id"],
                        "profile": json.loads(row["profile"]),
                        "status": row["status"],
                        "metrics": json.loads(row["metrics"]),
                        "tags": json.loads(row["tags"]),
                        "notes": row["notes"],
                        "cooldown_until": row["cooldown_until"],
                        "domain_registered": json.loads(row["domain_registered"]),
                        "metadata": json.loads(row["metadata"]),
                    })
                return None
                
        except sqlite3.Error as e:
            logger.error(f"Failed to get account {account_id}: {e}")
            return None
    
    def select_account(
        self,
        strategy: SelectionStrategy = SelectionStrategy.LEAST_RECENTLY_USED,
        tags: Optional[List[str]] = None,
        exclude_ids: Optional[List[str]] = None,
    ) -> Optional[AccountEntry]:
        """
        Select an available account based on strategy
        
        Args:
            strategy: Selection strategy to use
            tags: Filter by tags (must have all specified tags)
            exclude_ids: Account IDs to exclude
            
        Returns:
            Selected AccountEntry or None if no available accounts
        """
        available = self.list_available_accounts(tags=tags, exclude_ids=exclude_ids)
        
        if not available:
            logger.warning("No available accounts in pool")
            return None
        
        if strategy == SelectionStrategy.RANDOM:
            return secrets.choice(available)
        
        elif strategy == SelectionStrategy.ROUND_ROBIN:
            if self._round_robin_index >= len(available):
                self._round_robin_index = 0
            account = available[self._round_robin_index]
            self._round_robin_index += 1
            return account
        
        elif strategy == SelectionStrategy.LEAST_RECENTLY_USED:
            return min(
                available,
                key=lambda a: a.metrics.last_used_at or "1970-01-01T00:00:00"
            )
        
        elif strategy == SelectionStrategy.MOST_SUCCESS_RATE:
            return max(
                available,
                key=lambda a: a.metrics.success_rate
            )
        
        elif strategy == SelectionStrategy.WEIGHTED:
            # Weighted random selection based on success rate
            weights = [max(a.metrics.success_rate, 0.1) for a in available]
            total = sum(weights)
            normalized = [w / total for w in weights]
            rand = secrets.SystemRandom().random()
            cumulative = 0
            for i, w in enumerate(normalized):
                cumulative += w
                if rand <= cumulative:
                    return available[i]
            return available[-1]
        
        return available[0]
    
    def list_available_accounts(
        self,
        tags: Optional[List[str]] = None,
        exclude_ids: Optional[List[str]] = None,
    ) -> List[AccountEntry]:
        """
        List all available accounts
        
        Args:
            tags: Filter by tags
            exclude_ids: Account IDs to exclude
            
        Returns:
            List of available AccountEntry objects
        """
        accounts = self.list_all_accounts()
        available = []
        
        for account in accounts:
            if exclude_ids and account.id in exclude_ids:
                continue
            if tags and not all(tag in account.tags for tag in tags):
                continue
            if account.is_available():
                available.append(account)
        
        return available
    
    def list_all_accounts(
        self,
        status: Optional[AccountStatus] = None,
        tags: Optional[List[str]] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> List[AccountEntry]:
        """
        List all accounts with optional filtering
        
        Args:
            status: Filter by status
            tags: Filter by tags
            limit: Maximum number of results
            offset: Number of results to skip
            
        Returns:
            List of AccountEntry objects
        """
        try:
            query = "SELECT * FROM accounts"
            params = []
            conditions = []
            
            if status:
                conditions.append("status = ?")
                params.append(status.value)
            
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            
            query += " ORDER BY created_at DESC"
            
            if limit is not None:
                query += " LIMIT ? OFFSET ?"
                params.extend([limit, offset])
            
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(query, params)
                rows = cursor.fetchall()
            
            accounts = []
            for row in rows:
                account = AccountEntry.from_dict({
                    "id": row["id"],
                    "profile": json.loads(row["profile"]),
                    "status": row["status"],
                    "metrics": json.loads(row["metrics"]),
                    "tags": json.loads(row["tags"]),
                    "notes": row["notes"],
                    "cooldown_until": row["cooldown_until"],
                    "domain_registered": json.loads(row["domain_registered"]),
                    "metadata": json.loads(row["metadata"]),
                })
                
                # Filter by tags in-memory (SQLite doesn't support array queries well)
                if tags and not all(tag in account.tags for tag in tags):
                    continue
                
                accounts.append(account)
            
            return accounts
            
        except sqlite3.Error as e:
            logger.error(f"Failed to list accounts: {e}")
            return []
    
    def update_account(
        self,
        account_id: str,
        status: Optional[AccountStatus] = None,
        tags: Optional[List[str]] = None,
        notes: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        cooldown_minutes: Optional[int] = None,
    ) -> bool:
        """
        Update account properties
        
        Args:
            account_id: Account ID to update
            status: New status
            tags: New tags
            notes: New notes
            metadata: Metadata to merge
            cooldown_minutes: Set cooldown period
            
        Returns:
            True if updated successfully
        """
        try:
            updates = []
            params = []
            
            if status is not None:
                updates.append("status = ?")
                params.append(status.value)
            
            if tags is not None:
                updates.append("tags = ?")
                params.append(json.dumps(tags))
            
            if notes is not None:
                updates.append("notes = ?")
                params.append(notes)
            
            if metadata is not None:
                # Merge with existing metadata
                account = self.get_account(account_id)
                if account:
                    current_metadata = account.metadata
                    current_metadata.update(metadata)
                    updates.append("metadata = ?")
                    params.append(json.dumps(current_metadata))
            
            if cooldown_minutes is not None:
                cooldown_until = (datetime.now() + timedelta(minutes=cooldown_minutes)).isoformat()
                updates.append("cooldown_until = ?")
                params.append(cooldown_until)
            
            if not updates:
                return False
            
            updates.append("updated_at = ?")
            params.append(datetime.now().isoformat())
            params.append(account_id)
            
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    f"UPDATE accounts SET {', '.join(updates)} WHERE id = ?",
                    params,
                )
                conn.commit()
            
            logger.debug(f"Account updated: {account_id}")
            return True
            
        except sqlite3.Error as e:
            logger.error(f"Failed to update account {account_id}: {e}")
            return False
    
    def record_usage(
        self,
        account_id: str,
        success: bool,
        domain: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """
        Record account usage for metrics tracking
        
        Args:
            account_id: Account ID
            success: Whether the operation was successful
            domain: Domain registered (if any)
            error: Error message (if failed)
        """
        try:
            account = self.get_account(account_id)
            if not account:
                logger.warning(f"Account not found: {account_id}")
                return
            
            metrics = account.metrics
            metrics.total_uses += 1
            metrics.last_used_at = datetime.now().isoformat()
            
            if success:
                metrics.successful_uses += 1
                metrics.total_successes += 1
                metrics.last_success_at = datetime.now().isoformat()
                if domain:
                    account.domain_registered.append(domain)
            else:
                metrics.failed_uses += 1
                metrics.total_failures += 1
                metrics.last_failure_at = datetime.now().isoformat()
                
                # Auto-suspend if failure rate is too high
                if metrics.total_uses >= 5 and metrics.failure_rate > 0.6:
                    account.status = AccountStatus.SUSPENDED.value
                    logger.warning(f"Account {account_id} auto-suspended due to high failure rate")
            
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    UPDATE accounts 
                    SET status = ?, metrics = ?, domain_registered = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        account.status,
                        json.dumps(asdict(metrics)),
                        json.dumps(account.domain_registered),
                        datetime.now().isoformat(),
                        account_id,
                    ),
                )
                
                conn.execute(
                    """
                    INSERT INTO account_events (account_id, event_type, event_data)
                    VALUES (?, ?, ?)
                    """,
                    (
                        account_id,
                        "usage_success" if success else "usage_failure",
                        json.dumps({
                            "success": success,
                            "domain": domain,
                            "error": error,
                            "timestamp": datetime.now().isoformat(),
                        }),
                    ),
                )
                conn.commit()
            
        except sqlite3.Error as e:
            logger.error(f"Failed to record usage for account {account_id}: {e}")
    
    def delete_account(self, account_id: str) -> bool:
        """
        Delete account from pool
        
        Args:
            account_id: Account ID to delete
            
        Returns:
            True if deleted successfully
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM account_events WHERE account_id = ?", (account_id,))
                conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
                conn.commit()
            
            logger.info(f"Account deleted: {account_id}")
            return True
            
        except sqlite3.Error as e:
            logger.error(f"Failed to delete account {account_id}: {e}")
            return False
    
    def get_pool_stats(self) -> Dict[str, Any]:
        """
        Get pool statistics
        
        Returns:
            Dictionary with pool statistics
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("SELECT status, COUNT(*) FROM accounts GROUP BY status")
                status_counts = dict(cursor.fetchall())
                
                cursor = conn.execute("SELECT COUNT(*) FROM accounts")
                total = cursor.fetchone()[0]
                
                cursor = conn.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")
                active_count = cursor.fetchone()[0]
                
                cursor = conn.execute("SELECT COUNT(*) FROM account_events")
                total_events = cursor.fetchone()[0]
            
            return {
                "total_accounts": total,
                "active_accounts": active_count,
                "available_accounts": len(self.list_available_accounts()),
                "status_breakdown": status_counts,
                "total_events": total_events,
                "pool_health": active_count / max(total, 1),
            }
            
        except sqlite3.Error as e:
            logger.error(f"Failed to get pool stats: {e}")
            return {}
    
    def get_account_history(
        self,
        account_id: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        Get account event history
        
        Args:
            account_id: Account ID
            limit: Maximum number of events
            
        Returns:
            List of event records
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    SELECT * FROM account_events 
                    WHERE account_id = ? 
                    ORDER BY created_at DESC 
                    LIMIT ?
                    """,
                    (account_id, limit),
                )
                rows = cursor.fetchall()
            
            return [
                {
                    "id": row["id"],
                    "event_type": row["event_type"],
                    "event_data": json.loads(row["event_data"]) if row["event_data"] else {},
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
            
        except sqlite3.Error as e:
            logger.error(f"Failed to get account history: {e}")
            return []
    
    def export_accounts(
        self,
        output_path: str,
        format: str = "json",
        status: Optional[AccountStatus] = None,
    ) -> int:
        """
        Export accounts to file
        
        Args:
            output_path: Output file path
            format: Export format ('json' or 'csv')
            status: Filter by status
            
        Returns:
            Number of accounts exported
        """
        accounts = self.list_all_accounts(status=status)
        
        if format == "json":
            data = [account.to_dict() for account in accounts]
            with open(output_path, "w") as f:
                json.dump(data, f, indent=2, default=str)
        
        elif format == "csv":
            import csv
            if not accounts:
                return 0
            
            with open(output_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["id", "username", "email", "status", "success_rate"])
                writer.writeheader()
                for account in accounts:
                    writer.writerow({
                        "id": account.id,
                        "username": account.profile.get("username", ""),
                        "email": account.profile.get("email", ""),
                        "status": account.status,
                        "success_rate": f"{account.metrics.success_rate:.2%}",
                    })
        
        logger.info(f"Exported {len(accounts)} accounts to {output_path}")
        return len(accounts)
    
    def health_check(self, account_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Perform health check on accounts
        
        Args:
            account_id: Optional account ID to check, if None checks all
            
        Returns:
            Health check results
        """
        accounts = [self.get_account(account_id)] if account_id else self.list_all_accounts()
        results = {}
        
        for account in accounts:
            if account is None:
                continue
            
            issues = []
            
            # Check success rate
            if account.metrics.total_uses >= 5 and account.metrics.success_rate < 0.5:
                issues.append("low_success_rate")
            
            # Check if recently failed
            if account.metrics.last_failure_at:
                last_fail = datetime.fromisoformat(account.metrics.last_failure_at)
                if datetime.now() - last_fail < timedelta(hours=24):
                    issues.append("recent_failure")
            
            # Check if cooling down
            if account.cooldown_until:
                cooldown_time = datetime.fromisoformat(account.cooldown_until)
                if datetime.now() < cooldown_time:
                    issues.append("in_cooldown")
            
            results[account.id] = {
                "healthy": len(issues) == 0,
                "issues": issues,
                "status": account.status,
                "success_rate": account.metrics.success_rate,
                "total_uses": account.metrics.total_uses,
            }
        
        return results
    
    def _log_event(self, account_id: str, event_type: str, event_data: Optional[Dict] = None) -> None:
        """Log an event for an account"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO account_events (account_id, event_type, event_data)
                    VALUES (?, ?, ?)
                    """,
                    (account_id, event_type, json.dumps(event_data) if event_data else None),
                )
                conn.commit()
        except sqlite3.Error:
            pass  # Event logging should not break main flow
    
    @contextmanager
    def _get_connection(self) -> Any:
        """Get database connection as context manager (for bridge/advanced queries)"""
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()
