"""
Pytest configuration and fixtures for DigitalPlat Auto Register tests
"""

import pytest
import asyncio
from typing import Generator
from pathlib import Path

from digitalplat_auto_register.types import DigitalPlatConfig, UserProfile, EmailCredentials
from digitalplat_auto_register.core.config import ConfigManager
from digitalplat_auto_register.services.turnstile_solver import TurnstileSolver
from digitalplat_auto_register.services.browser_automation import BrowserAutomationService


@pytest.fixture
def test_config() -> DigitalPlatConfig:
    """Provide test configuration"""
    config_dict = {
        "base_url": "https://test.digitalplat.org",
        "registration_endpoint": "/auth/register",
        "turnstile": {
            "enabled": True,
            "solver_type": "mock",
            "timeout": 10,
            "max_retries": 1,
        },
        "email": {
            "provider": "mock",
            "timeout": 30,
        },
        "browser": {
            "headless": True,
            "timeout": 10000,
            "wait_for_timeout": 5000,
        },
        "max_registration_attempts": 1,
        "verification_timeout": 30,
        "verification_check_interval": 2,
    }
    return DigitalPlatConfig(**config_dict)


@pytest.fixture
def test_user_profile() -> UserProfile:
    """Provide test user profile data"""
    return UserProfile(
        username="testuser_12345",
        email="test@example.com",
        fullname="Test User",
        phone="+1-555-123-4567",
        password="TestPass123!",
        referral_code="testcode123"
    )


@pytest.fixture
def mock_turnstile_config() -> dict:
    """Mock Turnstile configuration for testing"""
    return {
        "enabled": True,
        "solver_type": "mock",
        "timeout": 10,
        "max_retries": 1,
        "sitekey": "0x4AAAAAAAxuMrGCYFcOwd1N",
    }


@pytest.fixture
def config_manager() -> ConfigManager:
    """Provide configuration manager"""
    return ConfigManager()


@pytest.fixture
def temp_config_file(tmp_path) -> Path:
    """Create a temporary configuration file for testing"""
    config_file = tmp_path / "test_config.yaml"
    config_data = {
        "base_url": "https://test.digitalplat.org",
        "turnstile": {
            "solver_type": "mock",
            "timeout": 10
        },
        "email": {
            "provider": "mock"
        },
        "browser": {
            "headless": True,
            "timeout": 10000
        }
    }
    
    import yaml
    with open(config_file, 'w') as f:
        yaml.dump(config_data, f)
    
    return config_file


@pytest.fixture
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Create an instance of the default event loop for each test case"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def sample_verification_email() -> str:
    """Sample verification email content for parsing tests"""
    return """
    <html>
    <body>
        <h1>DigitalPlat Verification</h1>
        <p>Hello, your DigitalPlat registration verification code is: <strong>123456</strong></p>
        <p>This code will expire in 10 minutes.</p>
    </body>
    </html>
    """


@pytest.fixture
def sample_registration_result() -> dict:
    """Sample registration result for testing serialization"""
    return {
        "success": True,
        "registration_id": "reg_test_123",
        "username": "testuser",
        "email": "test@example.com",
        "email_verified": True,
        "account_created": True,
        "registration_status": "completed",
        "step_results": [
            {
                "name": "turnstile_token_acquisition",
                "status": "completed",
                "success": True,
                "duration": 2.5,
                "message": "Token acquired"
            }
        ]
    }


# Mock external service fixtures


@pytest.fixture
def mock_turnstile_solver(mocker):
    """Mock Turnstile solver for testing"""
    mock_solver = mocker.AsyncMock(spec=TurnstileSolver)
    mock_solver.get_token.return_value = {
        "success": True,
        "token": "mock_token_123",
        "created_at": "2024-01-01T00:00:00",
        "expires_at": "2024-01-01T00:05:00"
    }
    return mock_solver


@pytest.fixture
def mock_email_service(mocker):
    """Mock email service for testing"""
    mock_service = mocker.AsyncMock()
    mock_service.create_temporary_email.return_value = {
        "success": True,
        "email": "temp@example.com",
        "created_at": "2024-01-01T00:00:00"
    }
    mock_service.check_verification_email.return_value = {
        "found": True,
        "code": "123456",
        "received_at": "2024-01-01T00:02:00"
    }
    return mock_service


@pytest.fixture
def mock_browser_service(mocker):
    """Mock browser automation service for testing"""
    mock_browser = mocker.AsyncMock(spec=BrowserAutomationService)
    mock_browser.navigate_to_registration.return_value = {
        "success": True,
        "url": "https://test.digitalplat.org/auth/register",
        "title": "Register - DigitalPlat"
    }
    mock_browser.fill_registration_form.return_value = {
        "success": True,
        "url": "https://test.digitalplat.org/auth/register"
    }
    mock_browser.submit_registration_form.return_value = {
        "success": True,
        "url": "https://test.digitalplat.org/auth/verify"
    }
    mock_browser.handle_verification_popup.return_value = {
        "success": True,
        "url": "https://test.digitalplat.org/auth/login"
    }
    return mock_browser


# Test environment setup


def pytest_sessionstart(session):
    """Run before the test session starts"""
    print("\n" + "="*60)
    print("DigitalPlat Auto Register Test Suite")
    print("="*60)


def pytest_sessionfinish(session, exitstatus):
    """Run after the entire test session finishes"""
    print("\n" + "="*60)
    print(f"Test session completed with status: {exitstatus}")
    print("="*60)


# Helper functions for tests


def is_playwright_available() -> bool:
    """Check if Playwright is available for browser tests"""
    try:
        from playwright.async_api import async_playwright
        return True
    except ImportError:
        return False


def skip_if_no_playwright():
    """Skip test if Playwright is not available"""
    return pytest.mark.skipif(
        not is_playwright_available(),
        reason="Playwright not installed - run 'pip install playwright'"
    )


def skip_integration_tests():
    """Skip integration tests in certain environments"""
    import os
    skip_integration = os.environ.get('SKIP_INTEGRATION', '').lower() == 'true'
    return pytest.mark.skipif(
        skip_integration,
        reason="Integration tests skipped (set SKIP_INTEGRATION=false to run)"
    )