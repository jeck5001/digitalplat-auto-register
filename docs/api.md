# API Reference

## Overview

This document provides detailed API reference for the DigitalPlat Auto Register package.

## Core Classes

### DigitalPlatRegistrar

Main orchestration class for registration process.

```python
class DigitalPlatRegistrar:
    def __init__(self, config: DigitalPlatConfig)
```

#### Methods

##### register_account

```python
async def register_account(
    self,
    username: Optional[str] = None,
    email: Optional[str] = None,
    fullname: Optional[str] = None,
    phone: Optional[str] = None,
    password: Optional[str] = None,
    address_line1: Optional[str] = None,
    address_line2: Optional[str] = None,
    city: Optional[str] = None,
    state: Optional[str] = None,
    postal_code: Optional[str] = None,
    country: Optional[str] = None,
    referral_code: str = "",
    on_step_complete: Optional[Callable[[StepResult], None]] = None
) -> RegistrationResult
```

Register a new DigitalPlat account.

**Parameters:**
- `username`: Account username (auto-generated if None)
- `email`: Email address (auto-created temporary if None)
- `fullname`: Full name for the account
- `phone`: Phone number
- `password`: Account password (auto-generated if None)
- `address_line1-2`: Address lines
- `city`, `state`, `postal_code`, `country`: Location information
- `referral_code`: Referral code to use
- `on_step_complete`: Optional callback for step completion

**Returns:** `RegistrationResult` with complete registration details

**Example:**

```python
registrar = DigitalPlatRegistrar(config)
result = await registrar.register_account(
    username="myuser",
    fullname="John Doe",
    phone="+1-555-123-4567",
    referral_code="abc123"
)
```

### ConfigManager

Configuration management class.

```python
class ConfigManager:
    def __init__(self)
```

#### Methods

##### load_from_dict

```python
def load_from_dict(self, config_dict: Dict[str, Any]) -> 'ConfigManager'
```

Load configuration from dictionary.

##### load_from_file

```python
def load_from_file(self, file_path: str) -> 'ConfigManager'
```

Load configuration from file (YAML/JSON/TOML).

##### load_from_env

```python
def load_from_env(self, env_file: Optional[str] = None) -> 'ConfigManager'
```

Load configuration from environment variables.

##### load

```python
def load(self) -> DigitalPlatConfig
```

Merge and return final configuration.

### TurnstileSolver

Token acquisition service.

```python
class TurnstileSolver:
    def __init__(self, config: TurnstileConfig)
```

#### Methods

##### get_token

```python
async def get_token(
    self,
    website_url: str,
    website_key: str,
    action: Optional[str] = None,
    data: Optional[str] = None,
    pagedata: Optional[str] = None,
    user_agent: Optional[str] = None
) -> TurnstileResult
```

Acquire Turnstile token.

**Parameters:**
- `website_url`: Website URL with Turnstile
- `website_key`: Turnstile sitekey
- `action`: Turnstile action (optional)
- `data`: Turnstile data (optional)
- `pagedata`: Page data (optional)
- `user_agent`: User agent string (optional)

**Returns:** `TurnstileResult` with token and metadata

### BrowserAutomationService

Browser automation service.

```python
class BrowserAutomationService:
    def __init__(self, browser_config: BrowserConfig, proxy_config: Optional[ProxyConfig] = None)
```

#### Methods

##### navigate_to_registration

```python
async def navigate_to_registration(self, base_url: str, referral_code: str = "") -> BrowserResult
```

Navigate to registration page.

##### fill_registration_form

```python
async def fill_registration_form(
    self,
    user_profile: UserProfile,
    turnstile_token: str
) -> BrowserResult
```

Fill registration form with data and token.

##### submit_registration_form

```python
async def submit_registration_form(self) -> BrowserResult
```

Submit the registration form.

##### handle_verification_popup

```python
async def handle_verification_popup(self, verification_code: str) -> BrowserResult
```

Handle email verification popup.

### EmailService

Abstract base class for email services.

```python
class EmailService(ABC):
    def __init__(self, config: EmailCredentials)
```

#### Methods

##### create_temporary_email

```python
async def create_temporary_email(self) -> EmailResult
```

Create temporary email address.

##### check_verification_email

```python
async def check_verification_email(
    self,
    email: str,
    timeout: int = 300,
    check_interval: int = 5
) -> VerificationEmailResult
```

Check for and retrieve verification email.

## Data Classes

### DigitalPlatConfig

Main configuration data class.

```python
class DigitalPlatConfig(BaseModel):
    base_url: HttpUrl
    registration_endpoint: str
    login_endpoint: str
    turnstile: TurnstileConfig
    email: EmailCredentials
    browser: BrowserConfig
    proxy: ProxyConfig
    logging: LoggingConfig
```

### UserProfile

User profile data structure.

```python
@dataclass
class UserProfile:
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
```

### RegistrationResult

Comprehensive registration result.

```python
@dataclass
class RegistrationResult:
    success: bool
    registration_id: str
    username: Optional[str]
    email: Optional[str]
    email_verified: bool
    account_created: bool
    registration_status: RegistrationStatus
    step_results: List[StepResult]
    # ... additional fields
```

#### Properties

- `is_complete`: Check if registration completed
- `steps_completed`: Number of completed steps
- `steps_successful`: Number of successful steps
- `steps_failed`: Number of failed steps

#### Methods

- `mark_success()`: Mark as successful
- `mark_failed(error, stage)`: Mark as failed
- `to_dict()`: Convert to dictionary
- `to_json()`: Convert to JSON string

## Enumerations

### RegistrationStatus

```python
class RegistrationStatus(str, Enum):
    PENDING = "pending"
    TURNSTILE_TOKEN_OBTAINED = "turnstile_token_obtained"
    EMAIL_CREATED = "email_created"
    FORM_SUBMITTED = "form_submitted"
    VERIFICATION_EMAIL_RECEIVED = "verification_email_received"
    EMAIL_VERIFIED = "email_verified"
    COMPLETED = "completed"
    FAILED = "failed"
```

### EmailProvider

```python
class EmailProvider(str, Enum):
    MAIL_TD = "mail.td"
    TEN_MINUTE_MAIL = "10minutemail"
    GUERRILLA_MAIL = "guerrillamail"
```

### TurnstileSolverType

```python
class TurnstileSolverType(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"
    MOCK = "mock"
```

## Utility Functions

### register_with_defaults

```python
async def register_with_defaults(
    referral_code: str = "",
    config_file: Optional[str] = None,
    **kwargs
) -> RegistrationResult
```

Convenience function for basic registration.

### Helper Functions

```python
# Username generation
def generate_random_username(length: int = 8, prefix: str = "user") -> str

# Password generation  
def generate_password(length: int = 12, include_symbols: bool = True) -> str

# Phone number generation
def generate_phone_number(country_code: str = "+1") -> str

# Email validation
def validate_email_address(email: str) -> bool

# Verification code extraction
def extract_verification_code(content: str) -> Optional[str]
```

## Exceptions

### Base Exception

```python
class DigitalPlatError(Exception)
```

### Specific Exceptions

```python
class ConfigurationError(DigitalPlatError)
class TurnstileSolverError(DigitalPlatError)
class EmailServiceError(DigitalPlatError)
class BrowserAutomationError(DigitalPlatError)
class RegistrationError(DigitalPlatError)
class VerificationError(DigitalPlatError)
class NetworkError(DigitalPlatError)
class TimeoutError(DigitalPlatError)
```

## CLI Module

### Main CLI

```python
# Command structure
digitalplat-register [OPTIONS] COMMAND [ARGS]...
```

### Available Commands

- `single`: Register single account
- `batch`: Batch registration from file
- `config-generate`: Generate sample configuration
- `version`: Show version information

## Decorators

### retry_async

```python
def retry_async(
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,)
)
```

Retry decorator for async functions.

### measure_time

```python
def measure_time(func)
```

Decorator to measure function execution time.

## Context Managers

The `DigitalPlatRegistrar`, `TurnstileSolver`, and `BrowserAutomationService` classes all support async context manager usage:

```python
async with DigitalPlatRegistrar(config) as registrar:
    result = await registrar.register_account(...)
```

## Logging

The package uses `loguru` for logging. Configure logging through:

- Configuration file
- Environment variables
- Programmatic configuration

Example configuration:

```yaml
logging:
  enabled: true
  level: INFO
  log_file: digitalplat_register.log
  format: "<green>{time}</green> | <level>{level}</level> | {message}"
  rotation: "10 MB"
  retention: "1 week"
```

## Configuration Schema

Complete configuration schema with all available options.

```yaml
# URLs and Endpoints
base_url: "https://dash.domain.digitalplat.org"
registration_endpoint: "/auth/register"
login_endpoint: "/auth/login"

# Turnstile Configuration
turnstile:
  enabled: true
  solver_type: "remote"  # local, remote, mock
  remote_endpoint: "http://192.168.5.35:5072"
  local_solver_path: null
  sitekey: "0x4AAAAAAAxuMrGCYFcOwd1N"
  timeout: 120
  max_retries: 3

# Email Configuration
email:
  provider: "mail.td"  # mail.td, 10minutemail, guerrillamail
  domain: ""
  api_key: null
  timeout: 300

# Browser Configuration
browser:
  headless: true
  timeout: 30000
  wait_for_timeout: 10000
  user_agent: null
  viewport_width: 1920
  viewport_height: 1080
  accept_downloads: true
  accept_insecure_certs: true

# Proxy Configuration
proxy:
  enabled: false
  server: null
  username: null
  password: null

# Logging Configuration
logging:
  enabled: true
  level: "INFO"  # DEBUG, INFO, WARNING, ERROR
  format: "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
  log_file: "digitalplat_register.log"
  rotation: "10 MB"
  retention: "1 week"

# Operation Settings
max_registration_attempts: 3
retry_delay: 5.0
verification_timeout: 300
verification_check_interval: 5

default_username_prefix: "user"
default_password_length: 12
require_email_verification: true
```