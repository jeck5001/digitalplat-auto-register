"""
Tests for AccountPool module
"""

import os
import pytest
from pathlib import Path

from digitalplat_auto_register.core.account_pool import (
    AccountPool,
    AccountEntry,
    AccountStatus,
    AccountMetrics,
    SelectionStrategy,
)
from digitalplat_auto_register.types import UserProfile


@pytest.fixture
def test_db_path(tmp_path):
    """Provide a temporary database path"""
    return str(tmp_path / "test_account_pool.db")


@pytest.fixture
def pool(test_db_path):
    """Provide an AccountPool instance"""
    return AccountPool(db_path=test_db_path)


@pytest.fixture
def sample_profile():
    """Provide a sample user profile"""
    return UserProfile(
        username="testuser123",
        email="test@example.com",
        fullname="Test User",
        phone="+1-555-123-4567",
        password="TestPass123!",
    )


class TestAccountPool:
    """Test suite for AccountPool"""
    
    def test_add_account(self, pool, sample_profile):
        """Test adding an account to the pool"""
        entry = pool.add_account(sample_profile)
        
        assert entry.id is not None
        assert entry.profile["username"] == "testuser123"
        assert entry.profile["email"] == "test@example.com"
        assert entry.status == AccountStatus.ACTIVE.value
        assert entry.is_available()
    
    def test_add_account_with_tags(self, pool, sample_profile):
        """Test adding account with tags"""
        entry = pool.add_account(
            sample_profile,
            tags=["premium", "verified"],
            notes="Test account",
        )
        
        assert "premium" in entry.tags
        assert "verified" in entry.tags
        assert entry.notes == "Test account"
    
    def test_duplicate_account_id_raises_error(self, pool, sample_profile):
        """Test that duplicate account IDs raise ValueError"""
        pool.add_account(sample_profile, account_id="custom_id_123")
        
        with pytest.raises(ValueError, match="already exists"):
            pool.add_account(sample_profile, account_id="custom_id_123")
    
    def test_get_account(self, pool, sample_profile):
        """Test retrieving an account by ID"""
        added = pool.add_account(sample_profile)
        retrieved = pool.get_account(added.id)
        
        assert retrieved is not None
        assert retrieved.id == added.id
        assert retrieved.profile["username"] == "testuser123"
    
    def test_get_nonexistent_account(self, pool):
        """Test retrieving a non-existent account returns None"""
        result = pool.get_account("nonexistent_id")
        assert result is None
    
    def test_list_all_accounts(self, pool):
        """Test listing all accounts"""
        profile1 = UserProfile(
            username="user1", email="user1@test.com",
            fullname="User 1", phone="+1-555-111-1111", password="Pass1!"
        )
        profile2 = UserProfile(
            username="user2", email="user2@test.com",
            fullname="User 2", phone="+1-555-222-2222", password="Pass2!"
        )
        
        pool.add_account(profile1)
        pool.add_account(profile2)
        
        accounts = pool.list_all_accounts()
        assert len(accounts) == 2
    
    def test_list_available_accounts(self, pool, sample_profile):
        """Test listing available accounts"""
        pool.add_account(sample_profile)
        
        available = pool.list_available_accounts()
        assert len(available) == 1
        assert available[0].is_available()
    
    def test_update_account_status(self, pool, sample_profile):
        """Test updating account status"""
        entry = pool.add_account(sample_profile)
        
        success = pool.update_account(
            entry.id,
            status=AccountStatus.SUSPENDED,
        )
        
        assert success is True
        updated = pool.get_account(entry.id)
        assert updated.status == AccountStatus.SUSPENDED.value
    
    def test_update_account_tags(self, pool, sample_profile):
        """Test updating account tags"""
        entry = pool.add_account(sample_profile)
        
        success = pool.update_account(
            entry.id,
            tags=["new_tag"],
        )
        
        assert success is True
        updated = pool.get_account(entry.id)
        assert "new_tag" in updated.tags
    
    def test_record_usage_success(self, pool, sample_profile):
        """Test recording successful usage"""
        entry = pool.add_account(sample_profile)
        
        pool.record_usage(entry.id, success=True, domain="example.com")
        
        updated = pool.get_account(entry.id)
        assert updated.metrics.total_uses == 1
        assert updated.metrics.successful_uses == 1
        assert "example.com" in updated.domain_registered
    
    def test_record_usage_failure(self, pool, sample_profile):
        """Test recording failed usage"""
        entry = pool.add_account(sample_profile)
        
        pool.record_usage(entry.id, success=False, error="Timeout")
        
        updated = pool.get_account(entry.id)
        assert updated.metrics.total_uses == 1
        assert updated.metrics.failed_uses == 1
        assert updated.metrics.failure_rate == 1.0
    
    def test_auto_suspend_on_high_failure_rate(self, pool, sample_profile):
        """Test automatic suspension on high failure rate"""
        entry = pool.add_account(sample_profile)
        
        # Record 5 failures (total_uses > 5 not met yet, need at least 6)
        for i in range(5):
            pool.record_usage(entry.id, success=False)
        
        # 6th failure triggers auto-suspend
        pool.record_usage(entry.id, success=False)
        
        updated = pool.get_account(entry.id)
        # With 6 failures, failure_rate = 1.0 > 0.6, and total_uses >= 5
        assert updated.status == AccountStatus.SUSPENDED.value
    
    def test_delete_account(self, pool, sample_profile):
        """Test deleting an account"""
        entry = pool.add_account(sample_profile)
        
        success = pool.delete_account(entry.id)
        assert success is True
        
        deleted = pool.get_account(entry.id)
        assert deleted is None
    
    def test_get_pool_stats(self, pool, sample_profile):
        """Test getting pool statistics"""
        pool.add_account(sample_profile)
        
        stats = pool.get_pool_stats()
        assert stats["total_accounts"] == 1
        assert stats["active_accounts"] == 1
        assert stats["available_accounts"] == 1
    
    def test_health_check(self, pool, sample_profile):
        """Test health check functionality"""
        entry = pool.add_account(sample_profile)
        health = pool.health_check()
        
        assert entry.id in health
        assert health[entry.id]["healthy"] is True
    
    def test_export_accounts_json(self, pool, sample_profile, tmp_path):
        """Test exporting accounts to JSON"""
        pool.add_account(sample_profile)
        
        output_path = str(tmp_path / "export.json")
        count = pool.export_accounts(output_path, format="json")
        
        assert count == 1
        assert os.path.exists(output_path)
    
    def test_export_accounts_csv(self, pool, sample_profile, tmp_path):
        """Test exporting accounts to CSV"""
        pool.add_account(sample_profile)
        
        output_path = str(tmp_path / "export.csv")
        count = pool.export_accounts(output_path, format="csv")
        
        assert count == 1
        assert os.path.exists(output_path)
    
    def test_select_account_random(self, pool):
        """Test random account selection"""
        for i in range(5):
            profile = UserProfile(
                username=f"user{i}", email=f"user{i}@test.com",
                fullname=f"User {i}", phone=f"+1-555-000-000{i}", password=f"Pass{i}!"
            )
            pool.add_account(profile)
        
        selected = pool.select_account(strategy=SelectionStrategy.RANDOM)
        assert selected is not None
    
    def test_select_account_round_robin(self, pool):
        """Test round-robin selection"""
        for i in range(3):
            profile = UserProfile(
                username=f"user{i}", email=f"user{i}@test.com",
                fullname=f"User {i}", phone=f"+1-555-000-000{i}", password=f"Pass{i}!"
            )
            pool.add_account(profile)
        
        selections = []
        for _ in range(3):
            acc = pool.select_account(strategy=SelectionStrategy.ROUND_ROBIN)
            selections.append(acc.id)
        
        # All three different accounts should be selected
        assert len(set(selections)) == 3
    
    def test_select_account_with_tags_filter(self, pool):
        """Test tag-based filtering"""
        profile1 = UserProfile(
            username="user1", email="user1@test.com",
            fullname="User 1", phone="+1-555-111-1111", password="Pass1!"
        )
        profile2 = UserProfile(
            username="user2", email="user2@test.com",
            fullname="User 2", phone="+1-555-222-2222", password="Pass2!"
        )
        
        pool.add_account(profile1, tags=["premium"])
        pool.add_account(profile2, tags=["free"])
        
        selected = pool.select_account(tags=["premium"])
        assert selected is not None
        assert "premium" in selected.tags
    
    def test_get_account_history(self, pool, sample_profile):
        """Test getting account event history"""
        entry = pool.add_account(sample_profile)
        pool.record_usage(entry.id, success=True)
        pool.record_usage(entry.id, success=False)
        
        history = pool.get_account_history(entry.id)
        assert len(history) >= 3  # add + 2 usages
    
    def test_bulk_add_accounts(self, pool):
        """Test bulk account addition"""
        profiles = [
            UserProfile(
                username=f"user{i}", email=f"user{i}@test.com",
                fullname=f"User {i}", phone=f"+1-555-000-000{i}", password=f"Pass{i}!"
            )
            for i in range(10)
        ]
        
        entries = pool.add_accounts_bulk(profiles, tags=["batch"])
        assert len(entries) == 10
        for entry in entries:
            assert "batch" in entry.tags


class TestAccountEntry:
    """Test suite for AccountEntry"""
    
    def test_from_profile(self, sample_profile):
        """Test creating AccountEntry from UserProfile"""
        entry = AccountEntry.from_profile(sample_profile)
        
        assert entry.id is not None
        assert entry.profile["username"] == "testuser123"
        assert entry.status == AccountStatus.ACTIVE.value
    
    def test_to_profile(self, sample_profile):
        """Test converting back to UserProfile"""
        entry = AccountEntry.from_profile(sample_profile)
        profile = entry.to_profile()
        
        assert isinstance(profile, UserProfile)
        assert profile.username == sample_profile.username
    
    def test_is_available_active(self):
        """Test is_available for active account"""
        entry = AccountEntry(
            id="test",
            profile={},
            status=AccountStatus.ACTIVE.value,
        )
        assert entry.is_available() is True
    
    def test_is_available_suspended(self):
        """Test is_available for suspended account"""
        entry = AccountEntry(
            id="test",
            profile={},
            status=AccountStatus.SUSPENDED.value,
        )
        assert entry.is_available() is False


class TestAccountMetrics:
    """Test suite for AccountMetrics"""
    
    def test_success_rate_empty(self):
        """Test success rate with no usage"""
        metrics = AccountMetrics()
        assert metrics.success_rate == 1.0  # Default is optimistic
        assert metrics.failure_rate == 0.0
    
    def test_success_rate_calculation(self):
        """Test success rate calculation"""
        metrics = AccountMetrics(total_uses=10, successful_uses=7, failed_uses=3)
        assert metrics.success_rate == 0.7
        assert metrics.failure_rate == 0.3
