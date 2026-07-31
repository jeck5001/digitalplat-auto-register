"""
Bridge layer between existing web Account system and new AccountPool.

This module provides seamless integration between:
- Existing JSON-based AccountStore (used by web app)
- New SQLite-based AccountPool (with advanced features)

It enables:
1. One-time migration of legacy data to new system
2. Automatic sync between old and new systems
3. Backward compatibility for web UI
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from .account import Account, AccountStatus, AccountStore, BatchRegistrationJob
from .account_pool import AccountPool, AccountEntry, AccountStatus as PoolAccountStatus, SelectionStrategy
from .statistics import StatisticsCollector, MetricType


# Mapping between old AccountStatus and new PoolAccountStatus
STATUS_MAPPING = {
    AccountStatus.ACTIVE: PoolAccountStatus.ACTIVE,
    AccountStatus.PENDING: PoolAccountStatus.ACTIVE,
    AccountStatus.REGISTERING: PoolAccountStatus.IN_USE,
    AccountStatus.FAILED: PoolAccountStatus.SUSPENDED,
    AccountStatus.EXPIRED: PoolAccountStatus.BANNED,
}


class AccountPoolBridge:
    """
    Bridge between legacy AccountStore and new AccountPool.
    
    This bridge allows:
    - Gradual migration from JSON to SQLite
    - Automatic two-way sync
    - Statistics collection from existing accounts
    """
    
    def __init__(
        self,
        account_store: AccountStore,
        pool_db_path: Optional[str] = None,
        stats_db_path: Optional[str] = None,
    ):
        """
        Initialize bridge
        
        Args:
            account_store: Existing AccountStore instance
            pool_db_path: Path to AccountPool database (default: /app/data/account_pool.db)
            stats_db_path: Path to StatisticsCollector database (default: /app/data/statistics.db)
        """
        self._store = account_store
        self._pool = AccountPool(db_path=pool_db_path or self._default_pool_path())
        self._stats = StatisticsCollector(db_path=stats_db_path or self._default_stats_path())
    
    @staticmethod
    def _default_pool_path() -> str:
        return os.getenv("ACCOUNT_POOL_PATH", "/app/data/account_pool.db")
    
    @staticmethod
    def _default_stats_path() -> str:
        return os.getenv("STATISTICS_PATH", "/app/data/statistics.db")
    
    def migrate_from_store(self) -> Dict[str, int]:
        """
        Migrate all accounts from AccountStore to AccountPool.
        
        After migration:
        - All accounts are imported to SQLite
        - Statistics are generated based on historical data
        - Original JSON data is preserved
        
        Returns:
            Dict with counts of migrated/failed records
        """
        accounts = self._store.get_all_accounts()
        migrated = 0
        skipped = 0
        failed = 0
        
        for account in accounts:
            try:
                # Check if account already exists in pool
                # We use email as unique identifier during migration
                existing = self._find_pool_account_by_email(account.email)
                if existing:
                    skipped += 1
                    continue
                
                # Convert to UserProfile
                from ..types import UserProfile
                profile = UserProfile(
                    username=account.username,
                    email=account.email,
                    fullname=account.fullname or account.username,
                    phone=account.phone or "",
                    password=account.password or "",
                    address_line1=account.address_line1 or "",
                    address_line2=account.address_line2 or "",
                    city=account.city or "",
                    state=account.state or "",
                    postal_code=account.postal_code or "",
                    country=account.country or "US",
                    referral_code=account.referral_code or "",
                )
                
                # Map status
                pool_status = self._map_status(account.status)
                
                # Create AccountEntry in pool
                entry = self._pool.add_account(
                    profile,
                    tags=self._derive_tags(account),
                    notes=f"Migrated from AccountStore on {datetime.now().isoformat()}",
                    metadata={
                        "legacy_id": account.id,
                        "legacy_status": account.status.value,
                        "email_verified": account.email_verified,
                        "account_created": account.account_created,
                    },
                )
                
                # Update metrics based on historical data
                self._sync_account_metrics(entry.id, account)
                
                # Record to statistics
                self._record_historical_stats(account)
                
                migrated += 1
                
            except Exception as e:
                logger.warning(f"Failed to migrate account {account.id}: {e}")
                failed += 1
        
        logger.info(f"Migration complete: {migrated} migrated, {skipped} skipped, {failed} failed")
        return {"migrated": migrated, "skipped": skipped, "failed": failed}
    
    def _find_pool_account_by_email(self, email: str) -> Optional[AccountEntry]:
        """Find account in pool by email"""
        if not email:
            return None
        accounts = self._pool.list_all_accounts()
        for acc in accounts:
            if acc.profile.get("email") == email:
                return acc
        return None
    
    def _map_status(self, old_status: AccountStatus) -> PoolAccountStatus:
        """Map old AccountStatus to new PoolAccountStatus"""
        return STATUS_MAPPING.get(old_status, PoolAccountStatus.ACTIVE)
    
    def _derive_tags(self, account: Account) -> List[str]:
        """Derive tags from legacy account data"""
        tags = []
        if account.email_verified:
            tags.append("verified")
        if account.account_created:
            tags.append("created")
        if account.referral_code:
            tags.append("referred")
        if account.metadata.get("steps"):
            tags.append("has_steps")
        return tags
    
    def _sync_account_metrics(self, pool_account_id: str, account: Account) -> None:
        """Sync historical metrics from legacy account to new pool account"""
        metrics = {
            "total_uses": account.retry_count + (1 if account.status == AccountStatus.ACTIVE else 0),
            "successful_uses": 1 if account.status == AccountStatus.ACTIVE else 0,
            "failed_uses": 1 if account.status == AccountStatus.FAILED else 0,
            "total_successes": 1 if account.status == AccountStatus.ACTIVE else 0,
            "total_failures": account.retry_count + (1 if account.status == AccountStatus.FAILED else 0),
        }
        
        if account.registered_at:
            metrics["last_used_at"] = account.registered_at
            metrics["last_success_at"] = account.registered_at
        if account.error:
            metrics["last_failure_at"] = account.updated_at
        
        # Update pool account metrics
        with self._pool._get_connection() as conn:
            conn.execute(
                "UPDATE accounts SET metrics = ?, updated_at = ? WHERE id = ?",
                (json.dumps(metrics), datetime.now().isoformat(), pool_account_id),
            )
            conn.commit()
    
    def _record_historical_stats(self, account: Account) -> None:
        """Record historical account data to statistics"""
        if account.status == AccountStatus.ACTIVE:
            self._stats.record_registration(
                username=account.username,
                email=account.email,
                success=True,
                metadata={"source": "migration", "legacy_id": account.id},
            )
        elif account.status == AccountStatus.FAILED:
            self._stats.record_registration(
                username=account.username,
                email=account.email,
                success=False,
                error_reason=account.error,
                metadata={"source": "migration", "legacy_id": account.id},
            )
    
    async def sync_new_account(self, account: Account) -> Optional[AccountEntry]:
        """
        Sync a newly created Account to AccountPool.
        
        Call this method after creating a new account via the web UI
        to also add it to the pool.
        
        Args:
            account: The newly created Account
            
        Returns:
            Created AccountEntry or None if failed
        """
        try:
            from ..types import UserProfile
            profile = UserProfile(
                username=account.username,
                email=account.email,
                fullname=account.fullname or account.username,
                phone=account.phone or "",
                password=account.password or "",
                referral_code=account.referral_code or "",
            )
            
            entry = self._pool.add_account(
                profile,
                tags=["web_created"],
                metadata={
                    "legacy_id": account.id,
                    "source": "web_ui",
                },
            )
            
            # Record to stats
            self._stats.record_metric(
                MetricType.REGISTRATION_ATTEMPT,
                labels={"source": "web_ui"},
            )
            
            return entry
            
        except Exception as e:
            logger.error(f"Failed to sync new account to pool: {e}")
            return None
    
    async def sync_account_result(
        self,
        account: Account,
        success: bool,
        error: Optional[str] = None,
    ) -> None:
        """
        Sync account registration result to pool and stats.
        
        Args:
            account: The Account that was processed
            success: Whether registration succeeded
            error: Error message if failed
        """
        # Find corresponding pool account
        pool_account = self._find_pool_account_by_email(account.email)
        pool_account_id = pool_account.id if pool_account else None
        
        # Record to statistics
        self._stats.record_registration(
            username=account.username,
            email=account.email,
            success=success,
            error_reason=error,
            metadata={"legacy_id": account.id},
        )
        
        # Update pool account if exists
        if pool_account_id:
            self._pool.record_usage(
                pool_account_id,
                success=success,
                error=error,
            )
    
    def get_pool_stats(self) -> Dict[str, Any]:
        """Get combined stats from pool and legacy store"""
        pool_stats = self._pool.get_pool_stats()
        legacy_counts = self._store.count_accounts()
        
        return {
            "pool": pool_stats,
            "legacy": legacy_counts,
            "active_accounts": pool_stats.get("active_accounts", 0) + legacy_counts.get("active", 0),
            "total_accounts": pool_stats.get("total_accounts", 0) + legacy_counts.get("total", 0),
        }
    
    def get_statistics(self, days: int = 30) -> Dict[str, Any]:
        """Get statistics summary"""
        return self._stats.get_summary(days=days).__dict__
    
    @property
    def pool(self) -> AccountPool:
        """Access the underlying AccountPool"""
        return self._pool
    
    @property
    def stats(self) -> StatisticsCollector:
        """Access the underlying StatisticsCollector"""
        return self._stats


def run_migration(
    accounts_path: Optional[str] = None,
    pool_db_path: Optional[str] = None,
    stats_db_path: Optional[str] = None,
) -> Dict[str, int]:
    """
    Convenience function to run migration standalone.
    
    Usage:
        from digitalplat_auto_register.core.bridge import run_migration
        result = run_migration()
    """
    store_path = accounts_path or AccountStore.DEFAULT_ACCOUNTS_PATH
    store = AccountStore(data_path=Path(store_path))
    
    # Load data synchronously
    import asyncio
    asyncio.get_event_loop().run_until_complete(store.load())
    
    bridge = AccountPoolBridge(store, pool_db_path, stats_db_path)
    return bridge.migrate_from_store()


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "migrate":
        result = run_migration()
        print(f"Migration result: {result}")
    else:
        print("Usage: python bridge.py migrate")
