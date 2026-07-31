"""
Web API routes for new features: Account Pool, Statistics, Enhanced Logging

This module provides FastAPI router with endpoints for:
- Account pool management (CRUD, health check, selection strategies)
- Statistics dashboard (summary, time series, recent activities)
- Log viewing and filtering
- Data migration from legacy JSON to new SQLite
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from loguru import logger

from .core.account_pool import AccountPool, AccountStatus, SelectionStrategy, AccountMetrics
from .core.statistics import StatisticsCollector, MetricType, DashboardSummary
from .utils.enhanced_logging import log_aggregator, perf_tracker


# Pydantic models for API responses

class PoolAccountResponse(BaseModel):
    """Account pool entry response"""
    id: str
    username: str
    email: str
    status: str
    tags: List[str]
    total_uses: int
    success_rate: float
    last_used_at: Optional[str]
    created_at: str


class PoolStatsResponse(BaseModel):
    """Pool statistics response"""
    total_accounts: int
    active_accounts: int
    available_accounts: int
    status_breakdown: Dict[str, int]
    pool_health: float


class StatsSummaryResponse(BaseModel):
    """Statistics summary response"""
    total_registrations: int
    successful_registrations: int
    failed_registrations: int
    registration_success_rate: float
    total_domains_registered: int
    total_emails_created: int
    avg_registration_duration: float
    top_failure_reasons: Dict[str, int]
    recent_activities: List[Dict[str, Any]]


class LogEventResponse(BaseModel):
    """Log event response"""
    timestamp: str
    level: str
    message: str
    context: Optional[Dict[str, Any]]


class MigrationResponse(BaseModel):
    """Migration result response"""
    migrated: int
    skipped: int
    failed: int


class AddToPoolRequest(BaseModel):
    """Request to add account to pool"""
    username: str
    email: str
    password: str
    fullname: str = ""
    phone: str = ""
    tags: List[str] = []


class UpdatePoolAccountRequest(BaseModel):
    """Request to update pool account"""
    status: Optional[str] = None
    tags: Optional[List[str]] = None
    notes: Optional[str] = None


def create_api_router(
    pool: AccountPool,
    stats: StatisticsCollector,
) -> APIRouter:
    """
    Create FastAPI router with all new feature endpoints.
    
    Usage:
        from fastapi import FastAPI
        from digitalplat_auto_register.web_routes import create_api_router
        
        app = FastAPI()
        router = create_api_router(pool, stats)
        app.include_router(router, prefix="/api/v2")
    
    Args:
        pool: AccountPool instance
        stats: StatisticsCollector instance
        
    Returns:
        APIRouter with all endpoints
    """
    router = APIRouter(tags=["v2"])
    
    # =========================================================================
    # Account Pool Endpoints
    # =========================================================================
    
    @router.get("/pool", response_model=PoolStatsResponse)
    async def get_pool_stats():
        """Get account pool statistics"""
        return pool.get_pool_stats()
    
    @router.get("/pool/accounts", response_model=List[PoolAccountResponse])
    async def list_pool_accounts(
        status: Optional[str] = Query(None, description="Filter by status"),
        tags: Optional[str] = Query(None, description="Comma-separated tags to filter"),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        """List accounts in pool with filtering"""
        status_enum = AccountStatus(status) if status else None
        
        tag_list = None
        if tags:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        
        accounts = pool.list_all_accounts(
            status=status_enum,
            tags=tag_list,
            limit=limit,
            offset=offset,
        )
        
        return [
            PoolAccountResponse(
                id=acc.id,
                username=acc.profile.get("username", ""),
                email=acc.profile.get("email", ""),
                status=acc.status,
                tags=acc.tags,
                total_uses=acc.metrics.total_uses,
                success_rate=acc.metrics.success_rate,
                last_used_at=acc.metrics.last_used_at,
                created_at=acc.metrics.created_at,
            )
            for acc in accounts
        ]
    
    @router.get("/pool/available", response_model=List[PoolAccountResponse])
    async def list_available_accounts(
        tags: Optional[str] = Query(None, description="Comma-separated tags"),
        limit: int = Query(50, ge=1, le=200),
    ):
        """List available accounts"""
        tag_list = None
        if tags:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        
        accounts = pool.list_available_accounts(tags=tag_list)[:limit]
        
        return [
            PoolAccountResponse(
                id=acc.id,
                username=acc.profile.get("username", ""),
                email=acc.profile.get("email", ""),
                status=acc.status,
                tags=acc.tags,
                total_uses=acc.metrics.total_uses,
                success_rate=acc.metrics.success_rate,
                last_used_at=acc.metrics.last_used_at,
                created_at=acc.metrics.created_at,
            )
            for acc in accounts
        ]
    
    @router.post("/pool/accounts")
    async def add_account_to_pool(request: AddToPoolRequest):
        """Add a new account to the pool"""
        from .types import UserProfile
        
        profile = UserProfile(
            username=request.username,
            email=request.email,
            password=request.password,
            fullname=request.fullname or request.username,
            phone=request.phone or "",
        )
        
        entry = pool.add_account(profile, tags=request.tags)
        return {"id": entry.id, "status": "added"}
    
    @router.get("/pool/accounts/{account_id}")
    async def get_pool_account(account_id: str):
        """Get account details from pool"""
        account = pool.get_account(account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        return account.to_dict()
    
    @router.patch("/pool/accounts/{account_id}")
    async def update_pool_account(account_id: str, request: UpdatePoolAccountRequest):
        """Update pool account"""
        account = pool.get_account(account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        
        status_enum = None
        if request.status:
            try:
                status_enum = AccountStatus(request.status)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid status: {request.status}")
        
        success = pool.update_account(
            account_id,
            status=status_enum,
            tags=request.tags,
            notes=request.notes,
        )
        
        if not success:
            raise HTTPException(status_code=400, detail="Failed to update account")
        
        return {"status": "updated"}
    
    @router.delete("/pool/accounts/{account_id}")
    async def delete_pool_account(account_id: str):
        """Delete account from pool"""
        if not pool.delete_account(account_id):
            raise HTTPException(status_code=404, detail="Account not found")
        return {"status": "deleted"}
    
    @router.get("/pool/health")
    async def pool_health_check():
        """Get health check results for all pool accounts"""
        return pool.health_check()
    
    @router.post("/pool/select")
    async def select_account(
        strategy: str = Query("least_recently_used", description="Selection strategy"),
        tags: Optional[str] = Query(None, description="Comma-separated tags"),
    ):
        """Select an account from pool based on strategy"""
        try:
            strategy_enum = SelectionStrategy(strategy)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid strategy: {strategy}")
        
        tag_list = None
        if tags:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        
        account = pool.select_account(strategy=strategy_enum, tags=tag_list)
        if not account:
            raise HTTPException(status_code=404, detail="No available accounts")
        
        return {
            "id": account.id,
            "username": account.profile.get("username"),
            "email": account.profile.get("email"),
            "success_rate": account.metrics.success_rate,
        }
    
    @router.get("/pool/export")
    async def export_accounts(
        format: str = Query("json", regex="^(json|csv)$"),
        status: Optional[str] = Query(None),
    ):
        """Export accounts from pool"""
        import tempfile
        from fastapi.responses import FileResponse
        
        status_enum = AccountStatus(status) if status else None
        
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=f".{format}",
            delete=False,
            prefix="account_export_",
        ) as f:
            output_path = f.name
        
        count = pool.export_accounts(output_path, format=format, status=status_enum)
        
        if count == 0:
            import os
            os.unlink(output_path)
            raise HTTPException(status_code=404, detail="No accounts to export")
        
        media_type = "application/json" if format == "json" else "text/csv"
        return FileResponse(
            output_path,
            media_type=media_type,
            filename=f"accounts_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{format}",
        )
    
    # =========================================================================
    # Statistics Endpoints
    # =========================================================================
    
    @router.get("/stats", response_model=StatsSummaryResponse)
    async def get_stats_summary(days: int = Query(7, ge=1, le=365)):
        """Get statistics summary"""
        summary = stats.get_summary(days=days)
        return StatsSummaryResponse(
            total_registrations=summary.total_registrations,
            successful_registrations=summary.successful_registrations,
            failed_registrations=summary.failed_registrations,
            registration_success_rate=summary.registration_success_rate,
            total_domains_registered=summary.total_domains_registered,
            total_emails_created=summary.total_emails_created,
            avg_registration_duration=summary.avg_registration_duration,
            top_failure_reasons=summary.top_failure_reasons,
            recent_activities=summary.recent_activities[:20],
        )
    
    @router.get("/stats/daily")
    async def get_daily_stats(days: int = Query(30, ge=1, le=365)):
        """Get daily statistics breakdown"""
        summary = stats.get_summary(days=days)
        return summary.daily_stats
    
    @router.get("/stats/hourly")
    async def get_hourly_stats(hours: int = Query(24, ge=1, le=168)):
        """Get hourly statistics breakdown"""
        summary = stats.get_summary(days=max(hours // 24 + 1, 1))
        return summary.hourly_stats
    
    @router.get("/stats/time-series")
    async def get_time_series(
        metric_type: str = Query("registration_success", description="Metric type"),
        hours: int = Query(24, ge=1, le=168),
    ):
        """Get time series data for a metric"""
        try:
            mt = MetricType(metric_type)
        except ValueError:
            valid_types = [m.value for m in MetricType]
            raise HTTPException(
                status_code=400,
                detail=f"Invalid metric type. Valid types: {valid_types}",
            )
        
        series = stats.get_time_series(mt, hours=hours)
        return [
            {"timestamp": point.timestamp, "value": point.value}
            for point in series
        ]
    
    @router.get("/stats/recent")
    async def get_recent_registrations(
        limit: int = Query(20, ge=1, le=100),
        success_only: bool = Query(False),
        failed_only: bool = Query(False),
    ):
        """Get recent registration records"""
        return stats.get_recent_registrations(
            limit=limit,
            success_only=success_only,
            failed_only=failed_only,
        )
    
    @router.get("/stats/export")
    async def export_stats(
        format: str = Query("json", regex="^(json|csv)$"),
        days: int = Query(30, ge=1, le=365),
    ):
        """Export statistics data"""
        import tempfile
        from fastapi.responses import FileResponse
        
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=f".{format}",
            delete=False,
            prefix="stats_export_",
        ) as f:
            output_path = f.name
        
        count = stats.export_data(output_path, format=format, days=days)
        
        if count == 0:
            import os
            os.unlink(output_path)
            raise HTTPException(status_code=404, detail="No data to export")
        
        media_type = "application/json" if format == "json" else "text/csv"
        return FileResponse(
            output_path,
            media_type=media_type,
            filename=f"stats_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{format}",
        )
    
    @router.post("/stats/cleanup")
    async def cleanup_old_stats():
        """Clean up old statistics data"""
        deleted = stats.cleanup_old_data()
        return {"deleted": deleted}
    
    # =========================================================================
    # Log Endpoints
    # =========================================================================
    
    @router.get("/logs", response_model=List[LogEventResponse])
    async def get_logs(
        limit: int = Query(50, ge=1, le=200),
        level: Optional[str] = Query(None, description="Filter by log level"),
        minutes: Optional[int] = Query(None, description="Show logs from last N minutes"),
    ):
        """Get recent log events"""
        events = log_aggregator.get_recent_events(
            limit=limit,
            level=level,
            since_minutes=minutes,
        )
        return [
            LogEventResponse(
                timestamp=event["timestamp"],
                level=event["level"],
                message=event["message"],
                context=event.get("context"),
            )
            for event in events
        ]
    
    @router.get("/logs/errors")
    async def get_error_summary():
        """Get error summary"""
        return log_aggregator.get_error_summary()
    
    @router.get("/logs/levels")
    async def get_log_level_counts():
        """Get count of log events by level"""
        return log_aggregator.get_level_counts()
    
    # =========================================================================
    # Performance Endpoints
    # =========================================================================
    
    @router.get("/performance")
    async def get_performance_stats():
        """Get performance tracking statistics"""
        return perf_tracker.get_all_stats()
    
    @router.get("/performance/{metric_name}")
    async def get_performance_metric(metric_name: str):
        """Get performance stats for a specific metric"""
        stats_data = perf_tracker.get_stats(metric_name)
        if stats_data.get("count") == 0:
            raise HTTPException(status_code=404, detail=f"No data for metric: {metric_name}")
        return stats_data
    
    @router.post("/performance/reset")
    async def reset_performance_stats():
        """Reset all performance tracking data"""
        perf_tracker.reset()
        return {"status": "reset"}
    
    # =========================================================================
    # System Endpoints
    # =========================================================================
    
    @router.get("/system/info")
    async def get_system_info():
        """Get system information"""
        import platform
        return {
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "timestamp": datetime.now().isoformat(),
        }
    
    @router.get("/system/health")
    async def system_health():
        """Comprehensive system health check"""
        pool_stats = pool.get_pool_stats()
        log_summary = log_aggregator.get_error_summary()
        
        return {
            "status": "healthy",
            "pool": {
                "total": pool_stats.get("total_accounts", 0),
                "available": pool_stats.get("available_accounts", 0),
                "health": pool_stats.get("pool_health", 0),
            },
            "logs": {
                "total_errors": log_summary["total_errors"],
                "level_counts": log_aggregator.get_level_counts(),
            },
            "timestamp": datetime.now().isoformat(),
        }
    
    return router
