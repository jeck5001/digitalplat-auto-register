"""
Services module for DigitalPlat Auto Register package

This module contains service classes that handle external integrations and
business logic for the DigitalPlat auto registration process.
"""

from .turnstile_solver import TurnstileSolver
from .email_service import EmailService, EmailServiceFactory
from .browser_automation import BrowserAutomationService

__all__ = [
    'TurnstileSolver',
    'EmailService', 
    'EmailServiceFactory',
    'BrowserAutomationService'
]