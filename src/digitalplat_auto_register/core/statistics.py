"""
Statistics Collection and Dashboard for DigitalPlat Auto Register

This module provides statistics tracking and aggregation including:
- Registration success/failure tracking
- Domain registration metrics
- Time-based aggregation (hourly, daily, weekly)
- Performance metrics
- Data export capabilities
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, asdict, field

from loguru import logger


class MetricType(str, Enum):
    """Types of metrics tracked"""
    REGISTRATION_ATTEMPT = "registration_attempt"
    REGISTRATION_SUCCESS = "registration_success"
    REGISTRATION_FAILURE = "registration_failure"
    DOMAIN_REGISTERED = "domain_registered"
    DOMAIN_CHECK = "domain_check"
    EMAIL_CREATED = "email_created"
    EMAIL_VERIFIED = "email_verified"
    TURNSTILE_SOLVED = "turnstile_solved"
    BROWSER_SESSION = "browser_session"
    API_CALL = "api_call"
    ERROR_OCCURRED = "error_occurred"


@dataclass
class MetricRecord:
    """A single metric record"""
    metric_type: str
    value: float = 1.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    labels: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TimeSeriesPoint:
    """A point in a time series"""
    timestamp: str
    value: float
    labels: Dict[str, str] = field(default_factory=dict)


@dataclass
class DashboardSummary:
    """Summary statistics for dashboard"""
    # Counters
    total_registrations: int = 0
    successful_registrations: int = 0
    failed_registrations: int = 0
    total_domains_registered: int = 0
    total_emails_created: int = 0
    total_turnstile_solved: int = 0
    total_errors: int = 0
    
    # Rates
    registration_success_rate: float = 0.0
    email_success_rate: float = 0.0
    turnstile_success_rate: float = 0.0
    
    # Averages
    avg_registration_duration: float = 0.0
    avg_email_wait_time: float = 0.0
    avg_turnstile_solve_time: float = 0.0
    
    # Time-bucketed data
    hourly_stats: Dict[str, Dict[str, int]] = field(default_factory=dict)
    daily_stats: Dict[str, Dict[str, int]] = field(default_factory=dict)
    
    # Recent activity
    recent_activities: List[Dict[str, Any]] = field(default_factory=list)
    
    # Top data
    top_failure_reasons: Dict[str, int] = field(default_factory=dict)
    top_domains: Dict[str, int] = field(default_factory=dict)


class StatisticsCollector:
    """
    Statistics collector with SQLite persistence
    
    Features:
    - Event-based metric recording
    - Time-series aggregation
    - Configurable retention periods
    - Data export capabilities
    """
    
    DEFAULT_DB_PATH = "statistics.db"
    DEFAULT_RETENTION_DAYS = 90
    
    def __init__(self, db_path: Optional[str] = None, retention_days: int = DEFAULT_RETENTION_DAYS):
        """
        Initialize statistics collector
        
        Args:
            db_path: Path to SQLite database file
            retention_days: Number of days to retain data
        """
        self.db_path = db_path or self.DEFAULT_DB_PATH
        self.retention_days = retention_days
        self._init_database()
    
    def _init_database(self) -> None:
        """Initialize SQLite database"""
        try:
            # Ensure parent directory exists
            db_dir = os.path.dirname(self.db_path)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
            
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        metric_type TEXT NOT NULL,
                        value REAL DEFAULT 1.0,
                        timestamp TEXT NOT NULL,
                        labels TEXT DEFAULT '{}',
                        metadata TEXT DEFAULT '{}',
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_metrics_type 
                    ON metrics(metric_type)
                """)
                
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_metrics_timestamp 
                    ON metrics(timestamp)
                """)
                
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_metrics_type_timestamp 
                    ON metrics(metric_type, timestamp)
                """)
                
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS registrations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT,
                        email TEXT,
                        domain TEXT,
                        success INTEGER,
                        duration REAL,
                        error_reason TEXT,
                        timestamp TEXT NOT NULL,
                        metadata TEXT DEFAULT '{}'
                    )
                """)
                
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_registrations_timestamp 
                    ON registrations(timestamp)
                """)
                
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_registrations_success 
                    ON registrations(success)
                """)
                
                conn.commit()
            
            logger.debug(f"Statistics database initialized at {self.db_path}")
            
        except sqlite3.Error as e:
            logger.error(f"Failed to initialize statistics database: {e}")
            raise
    
    def record_metric(
        self,
        metric_type: MetricType,
        value: float = 1.0,
        labels: Optional[Dict[str, str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        timestamp: Optional[str] = None,
    ) -> None:
        """
        Record a metric data point
        
        Args:
            metric_type: Type of metric
            value: Metric value
            labels: Optional labels for categorization
            metadata: Optional additional metadata
            timestamp: Optional custom timestamp
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO metrics (metric_type, value, timestamp, labels, metadata)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        metric_type.value,
                        value,
                        timestamp or datetime.now().isoformat(),
                        json.dumps(labels or {}),
                        json.dumps(metadata or {}),
                    ),
                )
                conn.commit()
                
        except sqlite3.Error as e:
            logger.error(f"Failed to record metric: {e}")
    
    def record_registration(
        self,
        username: str,
        email: str,
        success: bool,
        duration: float = 0.0,
        domain: Optional[str] = None,
        error_reason: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Record a registration attempt
        
        Args:
            username: Username used
            email: Email used
            success: Whether registration succeeded
            duration: Duration in seconds
            domain: Domain registered (if any)
            error_reason: Error reason (if failed)
            metadata: Additional metadata
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO registrations 
                    (username, email, domain, success, duration, error_reason, timestamp, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        username,
                        email,
                        domain,
                        1 if success else 0,
                        duration,
                        error_reason,
                        datetime.now().isoformat(),
                        json.dumps(metadata or {}),
                    ),
                )
                conn.commit()
            
            # Also record as metrics
            self.record_metric(
                MetricType.REGISTRATION_ATTEMPT,
                labels={"success": str(success).lower()},
            )
            
            if success:
                self.record_metric(MetricType.REGISTRATION_SUCCESS)
                if domain:
                    self.record_metric(
                        MetricType.DOMAIN_REGISTERED,
                        labels={"domain": domain},
                    )
            else:
                self.record_metric(
                    MetricType.REGISTRATION_FAILURE,
                    labels={"reason": error_reason or "unknown"},
                )
                
        except sqlite3.Error as e:
            logger.error(f"Failed to record registration: {e}")
    
    def get_summary(self, days: int = 30) -> DashboardSummary:
        """
        Get dashboard summary statistics
        
        Args:
            days: Number of days to include
            
        Returns:
            DashboardSummary with aggregated statistics
        """
        summary = DashboardSummary()
        since = (datetime.now() - timedelta(days=days)).isoformat()
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Total registrations
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM registrations WHERE timestamp >= ?",
                    (since,),
                )
                summary.total_registrations = cursor.fetchone()[0]
                
                # Successful registrations
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM registrations WHERE timestamp >= ? AND success = 1",
                    (since,),
                )
                summary.successful_registrations = cursor.fetchone()[0]
                
                # Failed registrations
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM registrations WHERE timestamp >= ? AND success = 0",
                    (since,),
                )
                summary.failed_registrations = cursor.fetchone()[0]
                
                # Success rate
                if summary.total_registrations > 0:
                    summary.registration_success_rate = (
                        summary.successful_registrations / summary.total_registrations
                    )
                
                # Domains registered
                cursor = conn.execute(
                    "SELECT COUNT(DISTINCT domain) FROM registrations WHERE timestamp >= ? AND domain IS NOT NULL",
                    (since,),
                )
                summary.total_domains_registered = cursor.fetchone()[0]
                
                # Total emails
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM metrics WHERE metric_type = ? AND timestamp >= ?",
                    (MetricType.EMAIL_CREATED.value, since),
                )
                summary.total_emails_created = cursor.fetchone()[0]
                
                # Turnstile solved
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM metrics WHERE metric_type = ? AND timestamp >= ?",
                    (MetricType.TURNSTILE_SOLVED.value, since),
                )
                summary.total_turnstile_solved = cursor.fetchone()[0]
                
                # Errors
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM metrics WHERE metric_type = ? AND timestamp >= ?",
                    (MetricType.ERROR_OCCURRED.value, since),
                )
                summary.total_errors = cursor.fetchone()[0]
                
                # Average durations
                cursor = conn.execute(
                    "SELECT AVG(duration) FROM registrations WHERE timestamp >= ? AND success = 1",
                    (since,),
                )
                row = cursor.fetchone()
                if row and row[0]:
                    summary.avg_registration_duration = round(row[0], 2)
                
                # Hourly stats (last 24 hours)
                hourly_since = (datetime.now() - timedelta(hours=24)).isoformat()
                cursor = conn.execute(
                    """
                    SELECT 
                        strftime('%Y-%m-%d %H:00:00', timestamp) as hour,
                        success,
                        COUNT(*) 
                    FROM registrations 
                    WHERE timestamp >= ?
                    GROUP BY hour, success
                    ORDER BY hour
                    """,
                    (hourly_since,),
                )
                
                hourly_stats: Dict[str, Dict[str, int]] = {}
                for row in cursor.fetchall():
                    hour = row[0]
                    if hour not in hourly_stats:
                        hourly_stats[hour] = {"success": 0, "failure": 0}
                    if row[1] == 1:
                        hourly_stats[hour]["success"] = row[2]
                    else:
                        hourly_stats[hour]["failure"] = row[2]
                
                summary.hourly_stats = hourly_stats
                
                # Daily stats
                cursor = conn.execute(
                    """
                    SELECT 
                        date(timestamp) as day,
                        success,
                        COUNT(*) 
                    FROM registrations 
                    WHERE timestamp >= ?
                    GROUP BY day, success
                    ORDER BY day
                    """,
                    (since,),
                )
                
                daily_stats: Dict[str, Dict[str, int]] = {}
                for row in cursor.fetchall():
                    day = row[0]
                    if day not in daily_stats:
                        daily_stats[day] = {"success": 0, "failure": 0}
                    if row[1] == 1:
                        daily_stats[day]["success"] = row[2]
                    else:
                        daily_stats[day]["failure"] = row[2]
                
                summary.daily_stats = daily_stats
                
                # Top failure reasons
                cursor = conn.execute(
                    """
                    SELECT error_reason, COUNT(*) as count 
                    FROM registrations 
                    WHERE timestamp >= ? AND success = 0 AND error_reason IS NOT NULL
                    GROUP BY error_reason 
                    ORDER BY count DESC 
                    LIMIT 10
                    """,
                    (since,),
                )
                summary.top_failure_reasons = dict(cursor.fetchall())
                
                # Top domains
                cursor = conn.execute(
                    """
                    SELECT domain, COUNT(*) as count 
                    FROM registrations 
                    WHERE timestamp >= ? AND domain IS NOT NULL
                    GROUP BY domain 
                    ORDER BY count DESC 
                    LIMIT 10
                    """,
                    (since,),
                )
                summary.top_domains = dict(cursor.fetchall())
                
                # Recent activities
                cursor = conn.execute(
                    """
                    SELECT username, email, success, timestamp 
                    FROM registrations 
                    ORDER BY timestamp DESC 
                    LIMIT 20
                    """,
                )
                summary.recent_activities = [
                    {
                        "username": row[0],
                        "email": row[1],
                        "success": bool(row[2]),
                        "timestamp": row[3],
                    }
                    for row in cursor.fetchall()
                ]
            
            return summary
            
        except sqlite3.Error as e:
            logger.error(f"Failed to get summary: {e}")
            return summary
    
    def get_time_series(
        self,
        metric_type: MetricType,
        hours: int = 24,
        bucket_minutes: int = 60,
    ) -> List[TimeSeriesPoint]:
        """
        Get time series data for a metric
        
        Args:
            metric_type: Type of metric
            hours: Number of hours to include
            bucket_minutes: Bucket size in minutes
            
        Returns:
            List of TimeSeriesPoint
        """
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    f"""
                    SELECT 
                        strftime('%Y-%m-%d %H:', timestamp) || 
                        printf('%02d', CAST(strftime('%M', timestamp) AS INTEGER) / ? * ?) as bucket,
                        SUM(value) as total
                    FROM metrics
                    WHERE metric_type = ? AND timestamp >= ?
                    GROUP BY bucket
                    ORDER BY bucket
                    """,
                    (bucket_minutes, bucket_minutes, metric_type.value, since),
                )
                
                return [
                    TimeSeriesPoint(
                        timestamp=row[0],
                        value=row[1],
                    )
                    for row in cursor.fetchall()
                ]
                
        except sqlite3.Error as e:
            logger.error(f"Failed to get time series: {e}")
            return []
    
    def get_recent_registrations(
        self,
        limit: int = 50,
        success_only: bool = False,
        failed_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Get recent registration records
        
        Args:
            limit: Maximum number of records
            success_only: Only return successful registrations
            failed_only: Only return failed registrations
            
        Returns:
            List of registration records
        """
        query = "SELECT * FROM registrations"
        params = []
        
        if success_only:
            query += " WHERE success = 1"
        elif failed_only:
            query += " WHERE success = 0"
        
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(query, params)
                rows = cursor.fetchall()
            
            return [
                {
                    "id": row["id"],
                    "username": row["username"],
                    "email": row["email"],
                    "domain": row["domain"],
                    "success": bool(row["success"]),
                    "duration": row["duration"],
                    "error_reason": row["error_reason"],
                    "timestamp": row["timestamp"],
                    "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
                }
                for row in rows
            ]
            
        except sqlite3.Error as e:
            logger.error(f"Failed to get recent registrations: {e}")
            return []
    
    def cleanup_old_data(self) -> int:
        """
        Remove data older than retention period
        
        Returns:
            Number of records deleted
        """
        cutoff = (datetime.now() - timedelta(days=self.retention_days)).isoformat()
        deleted = 0
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "DELETE FROM metrics WHERE timestamp < ?",
                    (cutoff,),
                )
                deleted += cursor.rowcount
                
                cursor = conn.execute(
                    "DELETE FROM registrations WHERE timestamp < ?",
                    (cutoff,),
                )
                deleted += cursor.rowcount
                
                conn.commit()
            
            if deleted > 0:
                logger.info(f"Cleaned up {deleted} old stat records")
            
            return deleted
            
        except sqlite3.Error as e:
            logger.error(f"Failed to cleanup old data: {e}")
            return 0
    
    def export_data(
        self,
        output_path: str,
        format: str = "json",
        days: int = 30,
    ) -> int:
        """
        Export statistics data
        
        Args:
            output_path: Output file path
            format: Export format ('json' or 'csv')
            days: Number of days to include
            
        Returns:
            Number of records exported
        """
        since = (datetime.now() - timedelta(days=days)).isoformat()
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT * FROM registrations WHERE timestamp >= ? ORDER BY timestamp DESC",
                    (since,),
                )
                rows = cursor.fetchall()
            
            records = [
                {
                    "id": row["id"],
                    "username": row["username"],
                    "email": row["email"],
                    "domain": row["domain"],
                    "success": bool(row["success"]),
                    "duration": row["duration"],
                    "error_reason": row["error_reason"],
                    "timestamp": row["timestamp"],
                }
                for row in rows
            ]
            
            if format == "json":
                with open(output_path, "w") as f:
                    json.dump(records, f, indent=2)
            
            elif format == "csv":
                import csv
                if records:
                    with open(output_path, "w", newline="") as f:
                        writer = csv.DictWriter(f, fieldnames=records[0].keys())
                        writer.writeheader()
                        writer.writerows(records)
            
            logger.info(f"Exported {len(records)} records to {output_path}")
            return len(records)
            
        except sqlite3.Error as e:
            logger.error(f"Failed to export data: {e}")
            return 0
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert summary to dictionary"""
        summary = self.get_summary()
        return asdict(summary)
