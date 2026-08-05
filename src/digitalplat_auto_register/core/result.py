"""
Result classes for DigitalPlat Auto Register

This module defines data structures and classes for representing the results
of various operations in the DigitalPlat auto registration process.
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from datetime import datetime
from enum import Enum

from ..types import RegistrationStatus, VerificationResult


@dataclass
class StepResult:
    """Result of a single registration step"""
    name: str
    status: str
    success: bool
    timestamp: datetime = field(default_factory=datetime.now)
    duration: Optional[float] = None
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            'name': self.name,
            'status': self.status,
            'success': self.success,
            'timestamp': self.timestamp.isoformat(),
            'duration': self.duration,
            'message': self.message,
            'details': self.details,
            'error': self.error
        }


@dataclass
class TurnstileResult:
    """Result of Turnstile token acquisition"""
    success: bool
    token: Optional[str] = None
    task_id: Optional[str] = None
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    solver_type: Optional[str] = None
    duration: Optional[float] = None
    error: Optional[str] = None
    
    @property
    def is_valid(self) -> bool:
        """Check if the token is still valid"""
        if not self.token or not self.expires_at:
            return False
        return datetime.now() < self.expires_at
    
    def time_until_expiry(self) -> Optional[float]:
        """Get time remaining until expiry in seconds"""
        if not self.expires_at:
            return None
        delta = self.expires_at - datetime.now()
        return max(0, delta.total_seconds())


@dataclass
class EmailResult:
    """Result of email operations"""
    success: bool
    email: Optional[str] = None
    provider: Optional[str] = None
    created_at: Optional[datetime] = None
    email_id: Optional[str] = None
    inbox_url: Optional[str] = None
    duration: Optional[float] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationEmailResult:
    """Result of verification email retrieval"""
    found: bool
    code: Optional[str] = None
    subject: Optional[str] = None
    sender: Optional[str] = None
    received_at: Optional[datetime] = None
    email_id: Optional[str] = None
    content: Optional[str] = None
    duration: Optional[float] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BrowserResult:
    """Result of browser automation operations"""
    success: bool
    url: Optional[str] = None
    title: Optional[str] = None
    screenshot: Optional[str] = None
    console_logs: List[str] = field(default_factory=list)
    duration: Optional[float] = None
    error: Optional[str] = None


@dataclass
class RegistrationResult:
    """
    Comprehensive result of a complete registration attempt
    """
    success: bool
    registration_id: str = field(default_factory=lambda: f"reg_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    
    # Registration details
    username: Optional[str] = None
    email: Optional[str] = None
    password: Optional[str] = None
    email_verified: bool = False
    account_created: bool = False
    registration_status: RegistrationStatus = RegistrationStatus.PENDING
    
    # Timing information
    start_time: datetime = field(default_factory=datetime.now)
    end_time: Optional[datetime] = None
    total_duration: Optional[float] = None
    
    # Step results
    turnstile_result: Optional[TurnstileResult] = None
    email_result: Optional[EmailResult] = None
    verification_result: Optional[VerificationEmailResult] = None
    browser_result: Optional[BrowserResult] = None
    
    # Overall process results
    step_results: List[StepResult] = field(default_factory=list)
    final_url: Optional[str] = None
    
    # Error information
    error: Optional[str] = None
    error_stage: Optional[str] = None
    retry_attempts: int = 0
    
    # Additional metadata
    referral_code: Optional[str] = None
    proxy_used: Optional[str] = None
    user_agent: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        """Initialize calculated fields"""
        if self.end_time and self.start_time:
            self.total_duration = (self.end_time - self.start_time).total_seconds()
    
    def mark_success(self) -> 'RegistrationResult':
        """Mark the registration as successful"""
        self.success = True
        self.registration_status = RegistrationStatus.COMPLETED
        self.end_time = datetime.now()
        self.total_duration = (self.end_time - self.start_time).total_seconds()
        return self
    
    def mark_failed(self, error: str, stage: Optional[str] = None) -> 'RegistrationResult':
        """Mark the registration as failed"""
        self.success = False
        self.registration_status = RegistrationStatus.FAILED
        self.error = error
        self.error_stage = stage
        self.end_time = datetime.now()
        self.total_duration = (self.end_time - self.start_time).total_seconds()
        return self
    
    def add_step_result(self, step_result: StepResult) -> 'RegistrationResult':
        """Add a step result to the registration"""
        self.step_results.append(step_result)
        return self
    
    def get_step_duration(self, step_name: str) -> Optional[float]:
        """Get the duration of a specific step"""
        for step in self.step_results:
            if step.name == step_name:
                return step.duration
        return None
    
    def get_successful_steps(self) -> List[StepResult]:
        """Get all successful steps"""
        return [step for step in self.step_results if step.success]
    
    def get_failed_steps(self) -> List[StepResult]:
        """Get all failed steps"""
        return [step for step in self.step_results if not step.success]
    
    @property
    def is_complete(self) -> bool:
        """Check if the registration process completed (success or failure)"""
        return self.success or self.registration_status == RegistrationStatus.FAILED
    
    @property
    def steps_completed(self) -> int:
        """Get the number of steps completed"""
        return len(self.step_results)
    
    @property
    def steps_successful(self) -> int:
        """Get the number of successful steps"""
        return len(self.get_successful_steps())
    
    @property
    def steps_failed(self) -> int:
        """Get the number of failed steps"""
        return len(self.get_failed_steps())
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert result to dictionary for serialization"""
        return {
            'success': self.success,
            'registration_id': self.registration_id,
            'username': self.username,
            'email': self.email,
            'email_verified': self.email_verified,
            'account_created': self.account_created,
            'registration_status': self.registration_status.value,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'total_duration': self.total_duration,
            'turnstile_result': self.turnstile_result.__dict__ if self.turnstile_result else None,
            'email_result': self.email_result.__dict__ if self.email_result else None,
            'verification_result': self.verification_result.__dict__ if self.verification_result else None,
            'browser_result': self.browser_result.__dict__ if self.browser_result else None,
            'step_results': [step.to_dict() for step in self.step_results],
            'final_url': self.final_url,
            'error': self.error,
            'error_stage': self.error_stage,
            'retry_attempts': self.retry_attempts,
            'referral_code': self.referral_code,
            'proxy_used': self.proxy_used,
            'user_agent': self.user_agent,
            'metadata': self.metadata,
            'is_complete': self.is_complete,
            'steps_completed': self.steps_completed,
            'steps_successful': self.steps_successful,
            'steps_failed': self.steps_failed,
        }
    
    def to_json(self) -> str:
        """Convert result to JSON string"""
        import json
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'RegistrationResult':
        """Create RegistrationResult from dictionary"""
        # This is a simplified implementation - in practice you might want
        # to fully reconstruct all the nested objects
        result = cls(
            success=data.get('success', False),
            registration_id=data.get('registration_id', ''),
            username=data.get('username'),
            email=data.get('email'),
            email_verified=data.get('email_verified', False),
            account_created=data.get('account_created', False),
            registration_status=RegistrationStatus(data.get('registration_status', 'pending')),
            error=data.get('error'),
            error_stage=data.get('error_stage'),
            retry_attempts=data.get('retry_attempts', 0),
            referral_code=data.get('referral_code'),
            proxy_used=data.get('proxy_used'),
            user_agent=data.get('user_agent'),
            metadata=data.get('metadata', {})
        )
        
        if data.get('start_time'):
            result.start_time = datetime.fromisoformat(data['start_time'])
        if data.get('end_time'):
            result.end_time = datetime.fromisoformat(data['end_time'])
        if data.get('total_duration'):
            result.total_duration = data['total_duration']
        
        return result


# Convenience factory functions for creating result objects

def create_success_result(username: str, email: str, referral_code: Optional[str] = None) -> RegistrationResult:
    """Create a successful registration result"""
    result = RegistrationResult(
        success=True,
        username=username,
        email=email,
        email_verified=True,
        account_created=True,
        referral_code=referral_code
    )
    return result.mark_success()


def create_failure_result(error: str, stage: Optional[str] = None, **kwargs) -> RegistrationResult:
    """Create a failed registration result"""
    result = RegistrationResult(**kwargs)
    return result.mark_failed(error, stage)