"""Test suite for DigitalPlat Auto Register package"""

import pytest

# Test configuration
TEST_CONFIG = {
    "base_url": "https://test.digitalplat.org",
    "turnstile": {
        "solver_type": "mock",  # Use mock for tests
        "timeout": 10,
    },
    "email": {
        "provider": "mock",  # Use mock email provider for tests
    },
    "browser": {
        "headless": True,
        "timeout": 10000,
    },
    "max_registration_attempts": 1,  # Don't retry in tests
    "verification_timeout": 30,
}


# Common test data
TEST_USER_DATA = {
    "username": "testuser_12345",
    "email": "test@example.com",
    "fullname": "Test User",
    "phone": "+1-555-123-4567",
    "password": "TestPass123!",
    "referral_code": "testcode"
}


def pytest_configure(config):
    """Configure pytest with custom markers"""
    config.addinivalue_line(
        "markers", "unit: Unit tests for internal components"
    )
    config.addinivalue_line(
        "markers", "integration: Integration tests that require external services"
    )
    config.addinivalue_line(
        "markers", "browser: Tests requiring browser automation"
    )
    config.addinivalue_line(
        "markers", "slow: Tests that are slow and might be skipped"
    )