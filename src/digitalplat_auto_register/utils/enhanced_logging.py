"""
Enhanced Logging for DigitalPlat Auto Register

This module provides enhanced logging capabilities:
- Structured JSON logging
- Log levels with color coding
- Scoped/contextual logging
- Performance tracking
- Log aggregation for dashboard
"""

import json
import logging
import os
import sys
import time
import traceback
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Type
from dataclasses import dataclass, asdict

from loguru import logger


class LogLevel(str, Enum):
    """Log levels with numeric values matching Python logging"""
    TRACE = "TRACE"
    DEBUG = "DEBUG"
    INFO = "INFO"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass
class LogContext:
    """Context information for structured logging"""
    operation: Optional[str] = None
    account_id: Optional[str] = None
    email: Optional[str] = None
    domain: Optional[str] = None
    request_id: Optional[str] = None
    session_id: Optional[str] = None
    extra: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.extra is None:
            self.extra = {}
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {k: v for k, v in asdict(self).items() if v is not None}


class StructuredLogFormatter:
    """Formats log entries as JSON for structured logging"""
    
    def __init__(self, include_context: bool = True):
        self.include_context = include_context
    
    def format(self, record: Dict[str, Any]) -> str:
        """Format log record as JSON"""
        log_entry = {
            "timestamp": record["time"].isoformat(),
            "level": record["level"].name,
            "message": record["message"],
            "module": record["name"],
            "function": record["function"],
            "line": record["line"],
        }
        
        if self.include_context:
            # Add context from extra fields
            context = {}
            for key in ["operation", "account_id", "email", "domain", "request_id", "session_id"]:
                if key in record["extra"]:
                    context[key] = record["extra"][key]
            if context:
                log_entry["context"] = context
        
        # Add exception info if present
        if record["exception"]:
            log_entry["exception"] = {
                "type": record["exception"].type,
                "value": str(record["exception"].value),
                "traceback": record["exception"].traceback,
            }
        
        return json.dumps(log_entry, default=str, ensure_ascii=False)


class LogAggregator:
    """
    Aggregates log events for dashboard display and analysis
    """
    
    def __init__(self, max_events: int = 1000):
        self.max_events = max_events
        self._events: List[Dict[str, Any]] = []
        self._error_counts: Dict[str, int] = defaultdict(int)
        self._handler_id = None
    
    def start(self) -> None:
        """Start capturing log events"""
        self._handler_id = logger.add(self._capture_sink, level="DEBUG")
    
    def stop(self) -> None:
        """Stop capturing log events"""
        if self._handler_id is not None:
            logger.remove(self._handler_id)
            self._handler_id = None
    
    def _capture_sink(self, message: Any) -> None:
        """Capture log message"""
        record = message.record
        
        event = {
            "timestamp": record["time"].isoformat(),
            "level": record["level"].name,
            "message": record["message"],
            "module": record.get("name", ""),
            "function": record.get("function", ""),
            "line": record.get("line", 0),
        }
        
        # Add context
        context = {}
        for key in ["operation", "account_id", "email", "domain", "request_id"]:
            if key in record["extra"]:
                context[key] = record["extra"][key]
        if context:
            event["context"] = context
        
        # Track errors
        if record["level"].name in ("ERROR", "CRITICAL"):
            error_type = f"{record.get('name', '')}.{record.get('function', '')}"
            self._error_counts[error_type] += 1
        
        self._events.append(event)
        
        # Trim to max size
        if len(self._events) > self.max_events:
            self._events = self._events[-self.max_events:]
    
    def get_recent_events(
        self,
        limit: int = 100,
        level: Optional[str] = None,
        since_minutes: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get recent log events
        
        Args:
            limit: Maximum number of events
            level: Filter by level
            since_minutes: Filter by time (minutes ago)
            
        Returns:
            List of log events
        """
        events = self._events
        
        if level:
            events = [e for e in events if e["level"] == level.upper()]
        
        if since_minutes:
            cutoff = datetime.now().timestamp() - (since_minutes * 60)
            events = [
                e for e in events
                if datetime.fromisoformat(e["timestamp"]).timestamp() >= cutoff
            ]
        
        return events[-limit:]
    
    def get_error_summary(self) -> Dict[str, Any]:
        """Get summary of errors"""
        return {
            "total_errors": sum(self._error_counts.values()),
            "error_by_source": dict(self._error_counts.most_common(10)),
            "recent_errors": [
                e for e in self._events if e["level"] in ("ERROR", "CRITICAL")
            ][-20:],
        }
    
    def get_level_counts(self) -> Dict[str, int]:
        """Get count of events by level"""
        counts = defaultdict(int)
        for event in self._events:
            counts[event["level"]] += 1
        return dict(counts)


class ContextualLogger:
    """
    Logger wrapper that adds context to all log messages
    """
    
    def __init__(self, context: Optional[LogContext] = None):
        self.context = context or LogContext()
        self._logger = logger
    
    def with_context(self, **kwargs) -> "ContextualLogger":
        """Create a new logger with additional context"""
        new_context = LogContext(
            operation=kwargs.get("operation", self.context.operation),
            account_id=kwargs.get("account_id", self.context.account_id),
            email=kwargs.get("email", self.context.email),
            domain=kwargs.get("domain", self.context.domain),
            request_id=kwargs.get("request_id", self.context.request_id),
            session_id=kwargs.get("session_id", self.context.session_id),
            extra={**self.context.extra, **{k: v for k, v in kwargs.items() 
                  if k not in ["operation", "account_id", "email", "domain", "request_id", "session_id"]}},
        )
        return ContextualLogger(new_context)
    
    def _log(self, level: str, message: str, **kwargs):
        """Log message with context"""
        extra = self.context.to_dict()
        extra.update(kwargs)
        
        log_func = getattr(self._logger, level.lower())
        log_func(message, **extra)
    
    def trace(self, message: str, **kwargs):
        self._log("TRACE", message, **kwargs)
    
    def debug(self, message: str, **kwargs):
        self._log("DEBUG", message, **kwargs)
    
    def info(self, message: str, **kwargs):
        self._log("INFO", message, **kwargs)
    
    def success(self, message: str, **kwargs):
        self._log("SUCCESS", message, **kwargs)
    
    def warning(self, message: str, **kwargs):
        self._log("WARNING", message, **kwargs)
    
    def error(self, message: str, **kwargs):
        self._log("ERROR", message, **kwargs)
    
    def critical(self, message: str, **kwargs):
        self._log("CRITICAL", message, **kwargs)
    
    def exception(self, message: str, exc: Exception, **kwargs):
        """Log exception with full traceback"""
        extra = self.context.to_dict()
        extra.update(kwargs)
        extra["exception_type"] = type(exc).__name__
        self._logger.exception(f"{message}: {exc}", **extra)


# Global log aggregator instance
log_aggregator = LogAggregator()


def setup_enhanced_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
    structured: bool = False,
    enable_aggregator: bool = True,
) -> LogAggregator:
    """
    Set up enhanced logging configuration
    
    Args:
        level: Log level
        log_file: Optional log file path
        structured: Enable JSON structured logging
        enable_aggregator: Enable log aggregation for dashboard
        
    Returns:
        LogAggregator instance
    """
    from loguru import logger
    
    # Remove default handler
    logger.remove()
    
    # Console handler with colors
    console_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    )
    
    logger.add(
        sys.stderr,
        level=level,
        format=console_format,
        colorize=sys.stderr.isatty(),
        backtrace=True,
        diagnose=True,
    )
    
    # File handler
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True) if os.path.dirname(log_file) else None
        
        if structured:
            formatter = StructuredLogFormatter()
            logger.add(
                log_file,
                level=level,
                format=formatter.format,
                rotation="10 MB",
                retention="1 week",
                encoding="utf-8",
            )
        else:
            logger.add(
                log_file,
                level=level,
                format=console_format.replace("<level>", "").replace("</level>", "")
                    .replace("<green>", "").replace("</green>", "")
                    .replace("<cyan>", "").replace("</cyan>", ""),
                rotation="10 MB",
                retention="1 week",
                encoding="utf-8",
            )
    
    # Start aggregator
    aggregator = LogAggregator()
    if enable_aggregator:
        aggregator.start()
    
    logger.info(f"Enhanced logging initialized | level={level} | file={log_file} | structured={structured}")
    return aggregator


def get_logger(
    name: str,
    context: Optional[LogContext] = None,
) -> ContextualLogger:
    """
    Get a contextual logger
    
    Args:
        name: Logger name (usually module name)
        context: Optional initial context
        
    Returns:
        ContextualLogger instance
    """
    if context is None:
        context = LogContext(operation=name)
    else:
        context.operation = context.operation or name
    
    return ContextualLogger(context)


@contextmanager
def log_timing(
    operation: str,
    logger: Optional[ContextualLogger] = None,
    level: str = "info",
):
    """
    Context manager for timing operations
    
    Usage:
        with log_timing("registration", my_logger):
            # do something
            pass
    """
    log = logger or get_logger("timing")
    start_time = time.time()
    
    try:
        yield log
        duration = time.time() - start_time
        getattr(log, level)(f"{operation} completed in {duration:.2f}s", operation=operation, duration=duration)
    except Exception as e:
        duration = time.time() - start_time
        log.error(f"{operation} failed after {duration:.2f}s", operation=operation, duration=duration)
        raise


def log_execution_time(func: Callable) -> Callable:
    """
    Decorator to log function execution time
    
    Usage:
        @log_execution_time
        async def my_function():
            pass
    """
    @wraps(func)
    async def async_wrapper(*args, **kwargs):
        start_time = time.time()
        try:
            result = await func(*args, **kwargs)
            duration = time.time() - start_time
            logger.debug(
                f"{func.__name__} completed in {duration:.2f}s",
                function=func.__name__,
                duration=duration,
            )
            return result
        except Exception as e:
            duration = time.time() - start_time
            logger.error(
                f"{func.__name__} failed after {duration:.2f}s: {e}",
                function=func.__name__,
                duration=duration,
            )
            raise
    
    @wraps(func)
    def sync_wrapper(*args, **kwargs):
        start_time = time.time()
        try:
            result = func(*args, **kwargs)
            duration = time.time() - start_time
            logger.debug(
                f"{func.__name__} completed in {duration:.2f}s",
                function=func.__name__,
                duration=duration,
            )
            return result
        except Exception as e:
            duration = time.time() - start_time
            logger.error(
                f"{func.__name__} failed after {duration:.2f}s: {e}",
                function=func.__name__,
                duration=duration,
            )
            raise
    
    if hasattr(func, "__await__"):
        return async_wrapper
    return sync_wrapper


class PerformanceTracker:
    """Tracks and reports performance metrics"""
    
    def __init__(self):
        self._metrics: Dict[str, List[float]] = defaultdict(list)
        self._counts: Dict[str, int] = defaultdict(int)
    
    def record(self, metric_name: str, duration: float) -> None:
        """Record a metric"""
        self._metrics[metric_name].append(duration)
        self._counts[metric_name] += 1
    
    def get_stats(self, metric_name: str) -> Dict[str, float]:
        """Get statistics for a metric"""
        values = self._metrics.get(metric_name, [])
        if not values:
            return {"count": 0}
        
        values.sort()
        count = len(values)
        
        return {
            "count": count,
            "min": min(values),
            "max": max(values),
            "avg": sum(values) / count,
            "p50": values[count // 2],
            "p95": values[int(count * 0.95)],
            "p99": values[int(count * 0.99)],
        }
    
    def get_all_stats(self) -> Dict[str, Dict[str, float]]:
        """Get all performance statistics"""
        return {name: self.get_stats(name) for name in self._metrics}
    
    def reset(self) -> None:
        """Reset all metrics"""
        self._metrics.clear()
        self._counts.clear()


# Global performance tracker
perf_tracker = PerformanceTracker()


def track_performance(metric_name: str):
    """
    Decorator to track function performance
    
    Usage:
        @track_performance("registration")
        async def register():
            pass
    """
    def decorator(func):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            start = time.time()
            try:
                return await func(*args, **kwargs)
            finally:
                duration = time.time() - start
                perf_tracker.record(metric_name, duration)
        
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            start = time.time()
            try:
                return func(*args, **kwargs)
            finally:
                duration = time.time() - start
                perf_tracker.record(metric_name, duration)
        
        if hasattr(func, "__await__"):
            return async_wrapper
        return sync_wrapper
    
    return decorator
