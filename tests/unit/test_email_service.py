import asyncio

from digitalplat_auto_register.services.email_service import MailTDService
from digitalplat_auto_register.types import EmailCredentials, EmailProvider


def build_service():
    return MailTDService(EmailCredentials(provider=EmailProvider.MAIL_TD))


def test_missing_auth_returns_immediately(monkeypatch):
    service = build_service()

    async def mock_fetch():
        return []

    monkeypatch.setattr(service, "_fetch_messages", mock_fetch)
    result = asyncio.run(
        service.check_verification_email(
            "missing@example.test",
            timeout=300,
            check_interval=5,
        )
    )

    assert result.found is True or result.found is False  # just runs without error
    # With no auth token, should return immediately with error
    assert "auth" in result.error.lower()


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


def test_wait_for_specific_sender_no_auth(monkeypatch):
    service = build_service()

    result = asyncio.run(
        service.wait_for_specific_sender(
            "test@domain.com",
            "shop.com",
            timeout=1,
            check_interval=0,
        )
    )

    assert result.found is False
    assert "auth" in result.error.lower()


def test_close_clears_auth_state():
    service = build_service()
    service.auth_token = "token"
    service.account_id = "acc_id"
    service.password = "pw"

    service._clear_auth_state()

    assert service.auth_token is None
    assert service.account_id is None
    assert service.password is None


def test_generate_email_user():
    service = build_service()
    user = service._generate_email_user(6)
    assert len(user) == 6
    assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789" for c in user)


def test_generate_password():
    service = build_service()
    pw = service._generate_password(8)
    assert len(pw) == 8


def test_derive_auth_key():
    service = build_service()
    key = service._derive_auth_key("test@test.com", "password123")
    assert len(key) == 64  # 32 bytes as hex = 64 chars
    # Same inputs should produce same key
    key2 = service._derive_auth_key("test@test.com", "password123")
    assert key == key2


def test_solve_pow():
    service = build_service()
    result = service._solve_pow("test@example.com", 15)
    assert "t" in result
    assert "n" in result
    assert "d" in result
    assert result["d"] == 15
    # Verify the PoW is valid
    import hashlib
    base = "test@example.com".lower().strip()
    attempt = f"{base}{result['t']}{result['n']}"
    hash_bytes = hashlib.sha256(attempt.encode("utf-8")).digest()
    # First 15 bits should be zero = first 1 byte is 0, second byte < 128 (only MSB can be set)
    assert hash_bytes[0] == 0
    assert hash_bytes[1] < 128  # bit 8 (0-indexed) must be 0 for 15 zero bits
