import asyncio
from types import SimpleNamespace

from digitalplat_auto_register.core.registrar import DigitalPlatRegistrar
from digitalplat_auto_register.core.result import (
    BrowserResult,
    EmailResult,
    RegistrationResult,
    TurnstileResult,
    VerificationEmailResult,
)
from digitalplat_auto_register.types import DigitalPlatConfig, RegistrationStatus


def test_prepare_user_profile_generates_required_defaults():
    registrar = DigitalPlatRegistrar(DigitalPlatConfig())

    profile = registrar._prepare_user_profile(
        username=None,
        email=None,
        fullname=None,
        phone=None,
        password=None,
        address_line1=None,
        address_line2=None,
        city=None,
        state=None,
        postal_code=None,
        country=None,
    )

    assert profile.username
    assert profile.password
    assert profile.fullname.count(" ") == 1
    assert profile.phone.startswith("+1-")
    assert profile.address_line1
    assert profile.city
    assert profile.state
    assert profile.postal_code
    assert profile.country == "US"


def test_prepare_user_profile_preserves_supplied_values():
    registrar = DigitalPlatRegistrar(DigitalPlatConfig())

    profile = registrar._prepare_user_profile(
        username="provided-user",
        email="provided@example.com",
        fullname="Provided User",
        phone="+1-555-555-5555",
        password="ProvidedPass1!",
        address_line1="1 Provided Street",
        address_line2="Suite 2",
        city="Austin",
        state="TX",
        postal_code="78701",
        country="US",
    )

    assert profile.username == "provided-user"
    assert profile.fullname == "Provided User"
    assert profile.phone == "+1-555-555-5555"
    assert profile.address_line1 == "1 Provided Street"
    assert profile.address_line2 == "Suite 2"
    assert profile.city == "Austin"
    assert profile.state == "TX"
    assert profile.postal_code == "78701"


def test_retry_recreates_auto_email_when_mailbox_session_was_cleaned(monkeypatch):
    registrar = DigitalPlatRegistrar(DigitalPlatConfig())
    registrar.registration_result = RegistrationResult(
        success=False,
        registration_status=RegistrationStatus.PENDING,
    )
    registrar._email_was_auto_created = True
    registrar.email_service = SimpleNamespace(
        current_email=None,
        auth_token=None,
        account_id=None,
    )
    profile = registrar._prepare_user_profile(
        username="retry-user",
        email="stale@qabq.com",
        fullname="Retry User",
        phone="+1-555-555-5555",
        password="RetryPass1!",
        address_line1="1 Retry Street",
        address_line2=None,
        city="Austin",
        state="TX",
        postal_code="78701",
        country="US",
    )
    created_emails = []

    async def no_op():
        return None

    async def create_email():
        created_emails.append("fresh@qabq.com")
        return EmailResult(success=True, email="fresh@qabq.com", provider="mail.td")

    monkeypatch.setattr(registrar, "_initialize_services", no_op)
    monkeypatch.setattr(registrar, "_cleanup_services", no_op)
    monkeypatch.setattr(
        registrar,
        "_acquire_turnstile_token",
        lambda: _async_result(TurnstileResult(success=True, token="token")),
    )
    monkeypatch.setattr(registrar, "_create_temporary_email", create_email)
    monkeypatch.setattr(
        registrar,
        "_navigate_to_registration",
        lambda referral_code: _async_result(
            BrowserResult(success=True, url="https://example.test/register")
        ),
    )
    monkeypatch.setattr(
        registrar,
        "_fill_and_submit_form",
        lambda user_profile, token: _async_result(BrowserResult(success=True)),
    )
    monkeypatch.setattr(
        registrar,
        "_wait_for_verification_email",
        lambda email: _async_result(VerificationEmailResult(found=True, code="654321")),
    )
    monkeypatch.setattr(
        registrar,
        "_handle_verification_popup",
        lambda code: _async_result(
            BrowserResult(success=True, url="https://example.test/done")
        ),
    )

    result = asyncio.run(registrar._execute_registration_workflow(profile, "ref", None))

    assert result.success is True
    assert created_emails == ["fresh@qabq.com"]
    assert profile.email == "fresh@qabq.com"


def _stub_workflow(monkeypatch, registrar, form_result):
    """Drive the workflow to the form step with everything else stubbed."""

    async def no_op():
        return None

    monkeypatch.setattr(registrar, "_initialize_services", no_op)
    monkeypatch.setattr(registrar, "_cleanup_services", no_op)
    monkeypatch.setattr(
        registrar,
        "_acquire_turnstile_token",
        lambda: _async_result(TurnstileResult(success=True, token="token")),
    )
    monkeypatch.setattr(
        registrar,
        "_create_temporary_email",
        lambda: _async_result(EmailResult(success=True, email="fresh@qabq.com", provider="mail.td")),
    )
    monkeypatch.setattr(
        registrar,
        "_navigate_to_registration",
        lambda referral_code: _async_result(
            BrowserResult(success=True, url="https://example.test/register")
        ),
    )
    monkeypatch.setattr(
        registrar,
        "_fill_and_submit_form",
        lambda user_profile, token: _async_result(form_result),
    )


def _profile(registrar):
    return registrar._prepare_user_profile(
        username="diag-user",
        email="diag@qabq.com",
        fullname="Diag User",
        phone="+1-555-555-5555",
        password="DiagPass1!",
        address_line1="1 Diag Street",
        address_line2=None,
        city="Austin",
        state="TX",
        postal_code="78701",
        country="US",
    )


def test_form_submission_failure_keeps_browser_reason(monkeypatch):
    """The job log must show why submission failed, not a fixed sentence.

    Filling and submitting fail for very different causes, and collapsing
    them into one message made every past failure undiagnosable.
    """
    registrar = DigitalPlatRegistrar(DigitalPlatConfig())
    registrar.registration_result = RegistrationResult(
        success=False,
        registration_status=RegistrationStatus.PENDING,
    )
    reason = (
        "Failed to fill registration form: Browser Automation Error: "
        "Required field address_line1 could not be filled"
    )
    _stub_workflow(monkeypatch, registrar, BrowserResult(success=False, error=reason))

    result = asyncio.run(
        registrar._execute_registration_workflow(_profile(registrar), "ref", None)
    )

    assert result.success is False
    assert result.error_stage == "form_submission"
    assert reason in result.error


def test_form_submission_failure_without_reason_still_reports_stage(monkeypatch):
    """A bare failure must not produce an empty error message."""
    registrar = DigitalPlatRegistrar(DigitalPlatConfig())
    registrar.registration_result = RegistrationResult(
        success=False,
        registration_status=RegistrationStatus.PENDING,
    )
    _stub_workflow(monkeypatch, registrar, BrowserResult(success=False))

    result = asyncio.run(
        registrar._execute_registration_workflow(_profile(registrar), "ref", None)
    )

    assert result.success is False
    assert result.error_stage == "form_submission"
    assert "Failed to submit registration form" in result.error


async def _async_result(value):
    return value
