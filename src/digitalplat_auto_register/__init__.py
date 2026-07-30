"""
DigitalPlat Auto Register - Automated DigitalPlat domain registration package

This package provides automated registration capabilities for DigitalPlat domains
using temporary email services and Cloudflare Turnstile bypass techniques.

Example usage:
    from digitalplat_auto_register import DigitalPlatRegistrar
    
    registrar = DigitalPlatRegistrar()
    result = registrar.register_account(
        username="testuser",
        email="temp@example.com",
        fullname="Test User",
        phone="+1-555-123-4567",
        password="SecurePass123!",
        referral_code="abc123"
    )
    
    if result.success:
        print(f"Account {result.username} created successfully!")
"""

__version__ = "0.1.0"
__author__ = "Auto-generated"
__email__ = "auto@example.com"
__description__ = "Automated DigitalPlat domain registration with temporary email verification"

from .core.registrar import DigitalPlatRegistrar
from .core.result import RegistrationResult
from .core.config import DigitalPlatConfig

__all__ = [
    "DigitalPlatRegistrar",
    "RegistrationResult", 
    "DigitalPlatConfig",
    "__version__",
    "__author__",
    "__email__",
    "__description__",
]