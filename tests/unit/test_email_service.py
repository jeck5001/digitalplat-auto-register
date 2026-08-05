import asyncio

from digitalplat_auto_register.services.email_service import MailTDService
from digitalplat_auto_register.types import EmailCredentials, EmailProvider


def run_async(coro):
    """Helper to run async code synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


def build_service():
    return MailTDService(EmailCredentials(provider=EmailProvider.MAIL_TD))


def test_missing_auth_returns_immediately(monkeypatch):
    service = build_service()

    async def page_unusable():
        return False

    async def failed_navigation():
        return False

    monkeypatch.setattr(service, "_page_is_usable", page_unusable)
    monkeypatch.setattr(service, "_navigate_to_inbox", failed_navigation)
    result = asyncio.run(
        service.check_verification_email(
            "missing@example.test",
            timeout=300,
            check_interval=5,
        )
    )

    assert result.found is False
    assert "unavailable" in result.error.lower() or "auth" in result.error.lower()
    assert result.duration < 1


def test_auth_recovery_before_polling(monkeypatch):
    service = build_service()
    service.auth_token = None
    service.account_id = None
    recovered = []

    async def page_unusable():
        return len(recovered) > 0

    async def recover():
        recovered.append(True)
        service.auth_token = "test_token"
        service.account_id = "test_account_id"
        return True

    async def fetch_messages():
        return [
            {
                "id": "msg_1",
                "subject": "Your verification code",
                "sender": {"name": "", "address": "noreply@digitalplat.com"},
                "text_body": "Your code is 654321",
            }
        ]

    async def fetch_detail(msg_id):
        return {
            "id": msg_id,
            "subject": "Your verification code",
            "sender": {"name": "", "address": "noreply@digitalplat.com"},
            "text_body": "Your verification code is 654321",
            "html_body": "",
        }

    monkeypatch.setattr(service, "_page_is_usable", page_unusable)
    monkeypatch.setattr(service, "_navigate_to_inbox", recover)
    monkeypatch.setattr(service, "_fetch_messages", fetch_messages)
    monkeypatch.setattr(service, "_fetch_message_detail", fetch_detail)
    result = asyncio.run(
        service.check_verification_email(
            "mailbox@example.test",
            timeout=1,
            check_interval=0,
        )
    )

    assert recovered == [True]
    assert result.found is True
    assert result.code == "654321"


def test_extract_code_from_message_text_body():
    service = build_service()

    message = {
        "text_body": "Your verification code is 123456.",
        "html_body": "",
    }

    code = service._extract_code_from_message(message)
    assert code == "123456"


def test_extract_code_from_message_html_body():
    service = build_service()

    message = {
        "text_body": "",
        "html_body": '<p>Verification code: <strong>789012</strong></p>',
    }

    code = service._extract_code_from_message(message)
    assert code == "789012"


def test_extract_code_from_chinese_message():
    service = build_service()

    message = {
        "text_body": "您的验证码是：987655",
        "html_body": "",
    }

    code = service._extract_code_from_message(message)
    assert code == "987655"


def test_is_verification_email():
    service = build_service()

    assert service._is_verification_email({
        "subject": "Verify your email",
        "sender": {"address": "noreply@example.com", "name": ""},
    })

    assert service._is_verification_email({
        "subject": "验证码已发送",
        "sender": {"address": "", "name": "DigitalPlat"},
    })

    assert not service._is_verification_email({
        "subject": "Weekly Newsletter",
        "sender": {"address": "news@example.com", "name": "News"},
    })


def test_wait_for_specific_sender_found(monkeypatch):
    service = build_service()
    service.auth_token = "test_token"
    service.account_id = "test_account"

    async def fetch_messages():
        return [
            {
                "id": "msg_a",
                "subject": "Newsletter",
                "sender": {"address": "news@example.com", "name": "News"},
            },
            {
                "id": "msg_b",
                "subject": "Special Offer",
                "sender": {"address": "offer@shop.com", "name": "Shop"},
            },
        ]

    async def fetch_detail(msg_id):
        return {"id": msg_id, "text_body": "content", "html_body": ""}

    monkeypatch.setattr(service, "_fetch_messages", fetch_messages)
    monkeypatch.setattr(service, "_fetch_message_detail", fetch_detail)

    result = asyncio.run(
        service.wait_for_specific_sender(
            "test@domain.com",
            "shop.com",
            timeout=1,
            check_interval=0,
        )
    )

    assert result.found is True
    assert "shop.com" in result.sender.lower()


def test_close_clears_auth_state():
    service = build_service()
    service.auth_token = "token"
    service.account_id = "acc_id"
    service.browser = None
    service.playwright = None

    # Simulate the state cleanup logic from close()
    # (browser is None so close_browser() part is a no-op)
    service._clear_auth_state()

    assert service.auth_token is None
    assert service.account_id is None
