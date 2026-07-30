"""
Type definitions for DigitalPlat Auto Register package

This module contains all type definitions, data classes, and Pydantic models
used throughout the digitalplat-auto-register package.
"""

from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Union
from datetime import datetime, timedelta
from enum import Enum

from pydantic import BaseModel, Field, HttpUrl, EmailStr


class RegistrationStatus(str, Enum):
    """Registration process status enumeration"""
    PENDING = "pending"
    TURNSTILE_TOKEN_OBTAINED = "turnstile_token_obtained"
    EMAIL_CREATED = "email_created"
    FORM_SUBMITTED = "form_submitted"
    VERIFICATION_EMAIL_RECEIVED = "verification_email_received"
    EMAIL_VERIFIED = "email_verified"
    COMPLETED = "completed"
    FAILED = "failed"


class VerificationResult(str, Enum):
    """Email verification result enumeration"""
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"


class TurnstileSolverType(str, Enum):
    """Available Turnstile solver service types"""
    LOCAL = "local"
    REMOTE = "remote"
    MOCK = "mock"


class EmailProvider(str, Enum):
    """Supported email providers"""
    MAIL_TD = "mail.td"
    TEN_MINUTE_MAIL = "10minutemail"
    GUERRILLA_MAIL = "guerrillamail"


@dataclass
class UserProfile:
    """User profile data structure"""
    username: str
    email: str
    fullname: str
    phone: str
    password: str
    address_line1: str = ""
    address_line2: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = "US"
    referral_code: str = ""


@dataclass
class TurnstileToken:
    """Turnstile token data structure"""
    token: str
    created_at: datetime
    expires_at: datetime
    sitekey: str = ""
    domain: str = ""
    
    @property
    def is_expired(self) -> bool:
        """Check if the token has expired"""
        return datetime.now() > self.expires_at
    
    def time_until_expiry(self) -> timedelta:
        """Get time remaining until token expires"""
        return max(timedelta(0), self.expires_at - datetime.now())


class EmailCredentials(BaseModel):
    """Email service configuration"""
    provider: EmailProvider = Field(default=EmailProvider.MAIL_TD, description="Email provider type")
    domain: str = Field(default="", description="Custom domain if needed")
    api_key: Optional[str] = Field(default=None, description="API key for provider")
    timeout: int = Field(default=300, description="Timeout for email operations")


class TurnstileConfig(BaseModel):
    """Turnstile solver configuration"""
    enabled: bool = Field(default=True, description="Enable Turnstile solving")
    solver_type: TurnstileSolverType = Field(
        default=TurnstileSolverType.MOCK, 
        description="Type of solver to use"
    )
    remote_endpoint: str = Field(
        default="http://192.168.5.35:5072", 
        description="Remote solver API endpoint"
    )
    local_solver_path: Optional[str] = Field(
        default=None, 
        description="Path to local solver executable"
    )
    sitekey: str = Field(
        default="0x4AAAAAAAxuMrGCYFcOwd1N", 
        description="Cloudflare Turnstile sitekey"
    )
    timeout: int = Field(default=120, description="Solver timeout in seconds")
    max_retries: int = Field(default=3, description="Maximum solver retry attempts")


class BrowserConfig(BaseModel):
    """Browser automation configuration"""
    engine: str = Field(default="chromium", description="Browser engine: chromium or camoufox")
    headless: bool = Field(default=True, description="Run browser in headless mode")
    timeout: int = Field(default=30000, description="Default timeout in milliseconds")
    wait_for_timeout: int = Field(default=10000, description="Wait for element timeout")
    user_agent: Optional[str] = Field(default=None, description="Custom user agent")
    viewport_width: int = Field(default=1920, description="Browser viewport width")
    viewport_height: int = Field(default=1080, description="Browser viewport height")
    accept_downloads: bool = Field(default=True, description="Allow downloads")
    accept_insecure_certs: bool = Field(default=True, description="Accept insecure certificates")


class ProxyConfig(BaseModel):
    """Proxy configuration for browser and requests"""
    enabled: bool = Field(default=False, description="Enable proxy")
    server: Optional[str] = Field(default=None, description="Proxy server URL")
    username: Optional[str] = Field(default=None, description="Proxy username")
    password: Optional[str] = Field(default=None, description="Proxy password")


class LoggingConfig(BaseModel):
    """Logging configuration"""
    enabled: bool = Field(default=True, description="Enable logging")
    level: str = Field(default="INFO", description="Logging level")
    format: str = Field(
        default="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        description="Log format string"
    )
    log_file: Optional[str] = Field(default="digitalplat_register.log", description="Log file path")
    rotation: str = Field(default="10 MB", description="Log file rotation size")
    retention: str = Field(default="1 week", description="Log file retention period")


class DigitalPlatConfig(BaseModel):
    """Main configuration model for DigitalPlat Auto Register"""
    # URLs
    base_url: HttpUrl = Field(
        default="https://dash.domain.digitalplat.org", 
        description="DigitalPlat dashboard base URL"
    )
    registration_endpoint: str = Field(
        default="/auth/register", 
        description="Registration page endpoint"
    )
    login_endpoint: str = Field(
        default="/auth/login", 
        description="Login page endpoint"
    )
    
    # Registration settings
    default_username_prefix: str = Field(
        default="user", 
        description="Default username prefix for automatic generation"
    )
    default_password_length: int = Field(
        default=12, 
        description="Default password length"
    )
    require_email_verification: bool = Field(
        default=True, 
        description="Email verification is required"
    )
    
    # Component configurations
    turnstile: TurnstileConfig = Field(
        default_factory=TurnstileConfig, 
        description="Turnstile solver configuration"
    )
    email: EmailCredentials = Field(
        default_factory=EmailCredentials, 
        description="Email service configuration"
    )
    browser: BrowserConfig = Field(
        default_factory=BrowserConfig, 
        description="Browser automation configuration"
    )
    proxy: ProxyConfig = Field(
        default_factory=ProxyConfig, 
        description="Proxy configuration"
    )
    logging: LoggingConfig = Field(
        default_factory=LoggingConfig, 
        description="Logging configuration"
    )
    
    # Operation settings
    max_registration_attempts: int = Field(
        default=3, 
        description="Maximum number of registration attempts"
    )
    retry_delay: float = Field(
        default=5.0, 
        description="Delay between retry attempts in seconds"
    )
    verification_timeout: int = Field(
        default=300, 
        description="Email verification timeout in seconds"
    )
    verification_check_interval: int = Field(
        default=5, 
        description="Interval between email checks in seconds"
    )
    
    class Config:
        """Pydantic configuration"""
        extra = "allow"
        json_schema_extra = {
            "example": {
                "base_url": "https://dash.domain.digitalplat.org",
                "registration_endpoint": "/auth/register",
                "turnstile": {
                    "solver_type": "remote",
                    "remote_endpoint": "http://192.168.5.35:5072"
                },
                "email": {
                    "provider": "mail.td"
                },
                "browser": {
                    "headless": True
                }
            }
        }
