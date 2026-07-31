import asyncio

from digitalplat_auto_register.services.email_service import MailTDService
from digitalplat_auto_register.types import EmailCredentials, EmailProvider


def build_service():
    return MailTDService(EmailCredentials(provider=EmailProvider.MAIL_TD))


def test_missing_mailbox_page_returns_immediately(monkeypatch):
    service = build_service()

    async def failed_navigation(email):
        return False

    monkeypatch.setattr(service, "_navigate_to_email_inbox", failed_navigation)
    result = asyncio.run(
        service.check_verification_email(
            "missing@example.test",
            timeout=300,
            check_interval=5,
        )
    )

    assert result.found is False
    assert result.error == "Temporary mailbox page is unavailable"
    assert result.duration < 1


def test_closed_mailbox_page_is_recovered_before_polling(monkeypatch):
    service = build_service()
    service.current_email = "mailbox@example.test"
    recovered = []

    async def page_is_usable():
        return bool(recovered)

    async def recover(email):
        recovered.append(email)
        return True

    async def find_email():
        class EmailElement:
            async def click(self):
                return None

        return EmailElement()

    async def extract_code():
        return "123456"

    monkeypatch.setattr(service, "_page_is_usable", page_is_usable)
    monkeypatch.setattr(service, "_navigate_to_email_inbox", recover)
    monkeypatch.setattr(service, "_find_digitalplat_email", find_email)
    monkeypatch.setattr(service, "_extract_verification_code", extract_code)
    result = asyncio.run(
        service.check_verification_email(
            "mailbox@example.test",
            timeout=1,
            check_interval=0,
        )
    )

    assert recovered == ["mailbox@example.test"]
    assert result.found is True
    assert result.code == "123456"
