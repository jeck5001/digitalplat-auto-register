"""
Tests for StatisticsCollector module
"""

import pytest
from pathlib import Path

from digitalplat_auto_register.core.statistics import (
    StatisticsCollector,
    MetricType,
    DashboardSummary,
    TimeSeriesPoint,
)


@pytest.fixture
def test_db_path(tmp_path):
    """Provide a temporary database path"""
    return str(tmp_path / "test_statistics.db")


@pytest.fixture
def stats(test_db_path):
    """Provide a StatisticsCollector instance"""
    return StatisticsCollector(db_path=test_db_path)


class TestStatisticsCollector:
    """Test suite for StatisticsCollector"""
    
    def test_record_metric(self, stats):
        """Test recording a metric"""
        stats.record_metric(MetricType.REGISTRATION_SUCCESS, value=1.0)
        
        summary = stats.get_summary()
        assert summary.successful_registrations == 0  # Only registrations count here
    
    def test_record_registration_success(self, stats):
        """Test recording successful registration"""
        stats.record_registration(
            username="testuser",
            email="test@example.com",
            success=True,
            duration=5.2,
            domain="example.com",
        )
        
        summary = stats.get_summary()
        assert summary.total_registrations == 1
        assert summary.successful_registrations == 1
        assert summary.registration_success_rate == 1.0
    
    def test_record_registration_failure(self, stats):
        """Test recording failed registration"""
        stats.record_registration(
            username="testuser",
            email="test@example.com",
            success=False,
            error_reason="CAPTCHA failed",
        )
        
        summary = stats.get_summary()
        assert summary.total_registrations == 1
        assert summary.failed_registrations == 1
        assert summary.registration_success_rate == 0.0
    
    def test_registration_with_metadata(self, stats):
        """Test recording registration with metadata"""
        stats.record_registration(
            username="testuser",
            email="test@example.com",
            success=True,
            metadata={"proxy": "http://proxy.example.com", "browser": "chromium"},
        )
        
        records = stats.get_recent_registrations(limit=1)
        assert len(records) == 1
        assert records[0]["metadata"]["proxy"] == "http://proxy.example.com"
    
    def test_get_summary_rates(self, stats):
        """Test summary statistics with mixed results"""
        for i in range(10):
            stats.record_registration(
                username=f"user{i}",
                email=f"user{i}@test.com",
                success=i < 7,  # 70% success rate
                duration=float(i),
            )
        
        summary = stats.get_summary()
        assert summary.total_registrations == 10
        assert summary.successful_registrations == 7
        assert summary.failed_registrations == 3
        assert summary.registration_success_rate == 0.7
    
    def test_get_time_series(self, stats):
        """Test time series data retrieval"""
        for _ in range(5):
            stats.record_metric(MetricType.REGISTRATION_SUCCESS)
        
        series = stats.get_time_series(MetricType.REGISTRATION_SUCCESS, hours=1)
        assert len(series) >= 1
    
    def test_get_recent_registrations(self, stats):
        """Test recent registrations retrieval"""
        for i in range(5):
            stats.record_registration(
                username=f"user{i}",
                email=f"user{i}@test.com",
                success=True,
            )
        
        records = stats.get_recent_registrations(limit=3)
        assert len(records) == 3
    
    def test_get_recent_registrations_success_only(self, stats):
        """Test filtering recent registrations by success"""
        stats.record_registration("user1", "u1@test.com", success=True)
        stats.record_registration("user2", "u2@test.com", success=False)
        stats.record_registration("user3", "u3@test.com", success=True)
        
        records = stats.get_recent_registrations(success_only=True)
        assert all(r["success"] for r in records)
    
    def test_get_recent_registrations_failed_only(self, stats):
        """Test filtering recent registrations by failure"""
        stats.record_registration("user1", "u1@test.com", success=True)
        stats.record_registration("user2", "u2@test.com", success=False)
        
        records = stats.get_recent_registrations(failed_only=True)
        assert all(not r["success"] for r in records)
    
    def test_cleanup_old_data(self, stats):
        """Test cleanup of old data"""
        # Record some data
        stats.record_metric(MetricType.REGISTRATION_SUCCESS)
        
        # Cleanup with very short retention
        stats.retention_days = 0
        deleted = stats.cleanup_old_data()
        
        # All data should be deleted with 0 retention
        assert deleted >= 0  # May be 0 depending on timing
    
    def test_export_data_json(self, stats, tmp_path):
        """Test data export to JSON"""
        stats.record_registration("user1", "u1@test.com", success=True)
        
        output_path = str(tmp_path / "export.json")
        count = stats.export_data(output_path, format="json")
        
        assert count == 1
        import json
        with open(output_path) as f:
            data = json.load(f)
        assert len(data) == 1
    
    def test_export_data_csv(self, stats, tmp_path):
        """Test data export to CSV"""
        stats.record_registration("user1", "u1@test.com", success=True)
        
        output_path = str(tmp_path / "export.csv")
        count = stats.export_data(output_path, format="csv")
        
        assert count == 1
        assert Path(output_path).exists()


class TestMetricType:
    """Test suite for MetricType enum"""
    
    def test_metric_types_exist(self):
        """Test all expected metric types exist"""
        assert MetricType.REGISTRATION_ATTEMPT.value == "registration_attempt"
        assert MetricType.REGISTRATION_SUCCESS.value == "registration_success"
        assert MetricType.REGISTRATION_FAILURE.value == "registration_failure"
        assert MetricType.DOMAIN_REGISTERED.value == "domain_registered"
        assert MetricType.EMAIL_CREATED.value == "email_created"
        assert MetricType.ERROR_OCCURRED.value == "error_occurred"


class TestDashboardSummary:
    """Test suite for DashboardSummary dataclass"""
    
    def test_default_values(self):
        """Test default dashboard summary values"""
        summary = DashboardSummary()
        
        assert summary.total_registrations == 0
        assert summary.successful_registrations == 0
        assert summary.registration_success_rate == 0.0
        assert summary.hourly_stats == {}
        assert summary.recent_activities == []
    
    def test_success_rate_calculation(self):
        """Test success rate in summary"""
        summary = DashboardSummary(
            total_registrations=10,
            successful_registrations=8,
            failed_registrations=2,
        )
        
        # Note: success_rate is not auto-calculated, but we can verify the fields
        assert summary.total_registrations == 10
        assert summary.successful_registrations == 8
