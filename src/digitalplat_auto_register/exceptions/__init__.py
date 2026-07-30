"""
Exception definitions for DigitalPlat Auto Register package

This module contains all custom exceptions used throughout the package.
"""


class DigitalPlatError(Exception):
    """Base exception class for all DigitalPlat Auto Register errors"""
    pass


class ConfigurationError(DigitalPlatError):
    """Raised when there's an error in package configuration"""
    def __init__(self, message: str, config_key: str = ""):
        self.config_key = config_key
        super().__init__(f"Configuration Error{f' ({config_key})' if config_key else ''}: {message}")


class TurnstileSolverError(DigitalPlatError):
    """Raised when Turnstile solving fails"""
    def __init__(self, message: str, task_id: str = "", error_code: str = ""):
        self.task_id = task_id
        self.error_code = error_code
        super().__init__(f"Turnstile Solver Error: {message}")


class EmailServiceError(DigitalPlatError):
    """Raised when email service operations fail"""
    def __init__(self, message: str, provider: str = "", email: str = ""):
        self.provider = provider
        self.email = email
        super().__init__(f"Email Service Error ({provider}): {message}")


class BrowserAutomationError(DigitalPlatError):
    """Raised when browser automation fails"""
    def __init__(self, message: str, page_url: str = "", selector: str = ""):
        self.page_url = page_url
        self.selector = selector
        super().__init__(f"Browser Automation Error: {message}")


class RegistrationError(DigitalPlatError):
    """Raised when registration process fails at any stage"""
    def __init__(self, message: str, stage: str = "", details: dict = None):
        self.stage = stage
        self.details = details or {}
        super().__init__(f"Registration Error ({stage}): {message}")


class VerificationError(DigitalPlatError):
    """Raised when email verification fails"""
    def __init__(self, message: str, email: str = "", timeout: bool = False):
        self.email = email
        self.timeout = timeout
        super().__init__(f"Verification Error{f' ({email})' if email else ''}: {message}")


class NetworkError(DigitalPlatError):
    """Raised when network operations fail"""
    def __init__(self, message: str, url: str = "", status_code: int = 0):
        self.url = url
        self.status_code = status_code
        super().__init__(f"Network Error: {message}")


class TimeoutError(DigitalPlatError):
    """Raised when operations timeout"""
    def __init__(self, message: str, timeout_seconds: float = 0, operation: str = ""):
        self.timeout_seconds = timeout_seconds
        self.operation = operation
        super().__init__(f"Timeout Error{f' ({operation})' if operation else ''}: {message}")


class ValidationError(DigitalPlatError):
    """Raised when data validation fails"""
    def __init__(self, message: str, field: str = "", value: str = ""):
        self.field = field
        self.value = value
        super().__init__(f"Validation Error{f' ({field})' if field else ''}: {message}")


# Convenience function to raise appropriate exceptions based on error context
def raise_appropriate_error(
    error_type: str,
    message: str,
    **kwargs
) -> None:
    """
    Raise appropriate exception based on error type
    
    Args:
        error_type: Type of error ('config', 'turnstile', 'email', 'browser', 'registration', 'verification', 'network', 'timeout', 'validation')
        message: Error message
        **kwargs: Additional arguments for the specific exception type
    """
    exception_map = {
        'config': ConfigurationError,
        'turnstile': TurnstileSolverError,
        'email': EmailServiceError,
        'browser': BrowserAutomationError,
        'registration': RegistrationError,
        'verification': VerificationError,
        'network': NetworkError,
        'timeout': TimeoutError,
        'validation': ValidationError,
    }
    
    exception_class = exception_map.get(error_type.lower(), DigitalPlatError)
    raise exception_class(message, **kwargs)