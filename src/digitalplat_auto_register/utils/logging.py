"""
Logging utilities for DigitalPlat Auto Register package

This module provides centralized logging configuration and utilities
used throughout the package.
"""

import sys
import os
from typing import Optional
from loguru import logger

from ..types import LoggingConfig


def setup_logging(config: LoggingConfig) -> None:
    """
    Set up logging configuration based on provided settings
    
    Args:
        config: Logging configuration
    """
    if not config.enabled:
        logger.disable()
        return
    
    # Remove default handler
    logger.remove()
    
    # Add console handler
    logger.add(
        sys.stderr,
        level=config.level,
        format=config.format,
        colorize=sys.stderr.isatty()
    )
    
    # Add file handler if specified
    if config.log_file:
        os.makedirs(os.path.dirname(config.log_file), exist_ok=True) if os.path.dirname(config.log_file) else None
        
        logger.add(
            config.log_file,
            level=config.level,
            format=config.format.replace('<level>{level: <8}</level>', '{level: <8}'),  # Remove colors for file
            rotation=config.rotation,
            retention=config.retention,
            encoding='utf-8'
        )
    
    logger.info(f"Logging initialized with level: {config.level}")


def get_logger(name: str): 
    """
    Get a logger with the specified name
    
    Args:
        name: Logger name (usually __name__)
        
    Returns:
        Configured logger instance
    """
    return logger.bind(name=name)


class LogCapture:
    """
    Context manager for capturing log messages
    
    Usage:
        with LogCapture() as capture:
            logger.info("This will be captured")
            logger.error("This too")
        
        print(capture.messages)  # List of captured messages
    """
    
    def __init__(self):
        self.messages = []
        self._handler_id = None
    
    def __enter__(self):
        def capture_sink(message):
            self.messages.append(message.record)
        
        self._handler_id = logger.add(capture_sink, level="DEBUG")
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._handler_id:
            logger.remove(self._handler_id)