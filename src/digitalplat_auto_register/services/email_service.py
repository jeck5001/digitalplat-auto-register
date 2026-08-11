"""
Email Service Module

This module provides functionality for managing temporary email addresses
and retrieving verification codes from various email providers.
"""

import asyncio
import time
import re
import json
import random
import string
import hashlib
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, List, Dict, Any

from playwright.async_api import Page as PlaywrightPage
import httpx
from loguru import logger
from argon2.low_level import hash_secret_raw, Type

from ..types import EmailCredentials, EmailProvider
from ..core.result import EmailResult, VerificationEmailResult
from ..exceptions import EmailServiceError


class EmailService(ABC):
    """
    Abstract base class for email services
    """

    def __init__(self, config: EmailCredentials):
        self.config = config
        self.current_email: Optional[str] = None
        self.email_created_at: Optional[datetime] = None

    @abstractmethod
    async def create_temporary_email(self) -> EmailResult:
        """
        Create a new temporary email address

        Returns:
            EmailResult with the created email details
        """
        pass

    @abstractmethod
    async def check_verification_email(
        self,
        email: str,
        timeout: int = 300,
        check_interval: int = 5
    ) -> VerificationEmailResult:
        """
        Check for and retrieve verification email

        Args:
            email: Email address to check
            timeout: Maximum time to wait for email in seconds
            check_interval: Time between checks in seconds

        Returns:
            VerificationEmailResult with the email content and verification code
        """
        pass

    @abstractmethod
    async def wait_for_specific_sender(
        self,
        email: str,
        sender_pattern: str,
        timeout: int = 300,
        check_interval: int = 5
    ) -> VerificationEmailResult:
        """
        Wait for email from specific sender

        Args:
            email: Email address to monitor
            sender_pattern: Pattern to match sender email/name
            timeout: Maximum time to wait in seconds
            check_interval: Time between checks in seconds

        Returns:
            VerificationEmailResult with the matched email
        """
        pass


class MailTDService(EmailService):
    """
    mail.td temporary email service implementation using pure HTTP API.

    The mail.td API uses:
    - Argon2id to derive auth_key from password (salt = SHA-256 of email)
    - SHA-256 proof-of-work for rate limiting (hash must start with N zero bits)
    - JWT tokens for authentication

    API endpoints:
    - GET /api/domains - List available email domains
    - POST /api/accounts - Create new email account (with PoW)
    - GET /api/accounts/{id}/messages?page=1 - List messages
    - GET /api/accounts/{id}/messages/{msg_id} - Get message detail
    """

    # Default PoW difficulty (from mail.td source code)
    DEFAULT_DIFFICULTY = 15
    # Characters used for random email username
    EMAIL_USER_CHARS = string.ascii_lowercase + string.digits
    # Characters used for random password
    PASSWORD_CHARS = string.ascii_letters + string.digits + "!@#$%"

    def __init__(self, config: EmailCredentials):
        """
        Initialize mail.td service

        Args:
            config: Email service configuration (provider must be EmailProvider.MAIL_TD)
        """
        if config.provider != EmailProvider.MAIL_TD:
            raise EmailServiceError(f"Invalid provider for MailTDService: {config.provider}")

        super().__init__(config)
        self.base_url = "https://mail.td"
        self.client: Optional[httpx.AsyncClient] = None

        # Auth state
        self.auth_token: Optional[str] = None
        self.account_id: Optional[str] = None
        self.password: Optional[str] = None
        self.domains: List[str] = []

        # PoW difficulty (auto-increases if server requests retry)
        self._current_difficulty = self.DEFAULT_DIFFICULTY
        self._retry_token: Optional[str] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client"""
        if self.client is None or self.client.is_closed:
            self.client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=30,
                follow_redirects=True,
            )
        return self.client

    async def _get_domains(self) -> List[dict]:
        """Fetch available email domains from mail.td.

        Returns list of domain info dicts sorted by preference:
        1. Default domain first (qabq.com)
        2. Other non-pro domains
        """
        client = await self._get_client()
        resp = await client.get("/api/domains")

        if resp.status_code != 200:
            raise EmailServiceError(f"Failed to get domains: {resp.status_code} {resp.text[:200]}")

        data = resp.json()
        domains = data.get("domains", [])

        if not domains:
            raise EmailServiceError("No available domains from mail.td")

        # Sort: default first, then non-pro domains, pro last
        default_domains = [d for d in domains if d.get("default")]
        free_domains = [d for d in domains if not d.get("default") and not d.get("pro_only")]
        pro_domains = [d for d in domains if not d.get("default") and d.get("pro_only")]

        sorted_domains = default_domains + free_domains + pro_domains
        self.domains = [d["domain"] for d in sorted_domains]
        return sorted_domains

    def _generate_email_user(self, length: int = 6) -> str:
        """Generate random email username"""
        return ''.join(random.choices(self.EMAIL_USER_CHARS, k=length))

    def _generate_password(self, length: int = 8) -> str:
        """Generate random password"""
        return ''.join(random.choices(self.PASSWORD_CHARS, k=length))

    @staticmethod
    def _derive_auth_key(email: str, password: str) -> str:
        """
        Derive auth_key using argon2id (same algorithm as mail.td JS code).

        mail.td derives auth_key = argon2id(password, salt=SHA-256(email))
        with parameters: time_cost=3, memory_cost=16384, parallelism=1, hash_length=32
        """
        salt = hashlib.sha256(email.lower().strip().encode()).digest()
        result = hash_secret_raw(
            secret=password.encode(),
            salt=salt,
            time_cost=3,
            memory_cost=16384,
            parallelism=1,
            hash_len=32,
            type=Type.ID,
        )
        return result.hex()

    @staticmethod
    def _solve_pow(address: str, difficulty: int) -> dict:
        """
        Solve SHA-256 proof-of-work challenge.

        Find a nonce such that SHA-256(address + timestamp + nonce) starts
        with `difficulty` zero bits.

        Returns:
            Dict with 't' (timestamp), 'n' (nonce), 'd' (difficulty)
        """
        base = address.lower().strip()
        timestamp = int(time.time())
        nonce = 0

        full_zero_bytes = difficulty // 8
        remaining_bits = difficulty % 8
        mask = (255 << (8 - remaining_bits)) & 255 if remaining_bits else 0

        while True:
            attempt = f"{base}{timestamp}{nonce}"
            hash_bytes = hashlib.sha256(attempt.encode("utf-8")).digest()

            valid = True
            for i in range(full_zero_bytes):
                if hash_bytes[i] != 0:
                    valid = False
                    break

            if valid and remaining_bits > 0 and full_zero_bytes < len(hash_bytes):
                if (hash_bytes[full_zero_bytes] & mask) != 0:
                    valid = False

            if valid:
                return {"t": timestamp, "n": str(nonce), "d": difficulty}
            nonce += 1

    async def create_temporary_email(self) -> EmailResult:
        """
        Create a new temporary email address using mail.td HTTP API.

        No browser needed - uses pure HTTP with Argon2id + SHA-256 PoW.

        Returns:
            EmailResult with the created email details
        """
        start_time = time.time()

        try:
            logger.info("Creating temporary email using mail.td HTTP API")

            # Get available domains
            domain_objects = await self._get_domains()
            # Pick the preferred domain (default first: qabq.com)
            domain_info = domain_objects[0]
            domain = domain_info["domain"]

            # Generate random email and password
            email_user = self._generate_email_user(10)
            email = f"{email_user}@{domain}"
            password = self._generate_password(8)

            logger.debug(f"Generated email: {email}, password: {password}")

            # Derive auth_key
            t0 = time.time()
            auth_key = self._derive_auth_key(email, password)
            logger.debug(f"Auth key derived ({time.time()-t0:.2f}s)")

            # Solve proof-of-work
            t0 = time.time()
            pow_result = self._solve_pow(email, self._current_difficulty)
            logger.debug(f"PoW solved: nonce={pow_result['n']}, difficulty={pow_result['d']} ({time.time()-t0:.3f}s)")

            # Create account via API
            client = await self._get_client()
            payload = {
                "address": email,
                "auth_key": auth_key,
                "pow": pow_result,
            }
            # Include retry token if retrying after server requested higher difficulty
            if self._retry_token:
                payload["pow"]["token"] = self._retry_token
                self._retry_token = None

            resp = await client.post("/api/accounts", json=payload)

            # Handle retry (server asks for higher difficulty)
            if resp.status_code != 201:
                resp_data = resp.json() if resp.text else {}
                if resp_data.get("status") == "retry":
                    new_diff = resp_data.get("required_difficulty", self._current_difficulty)
                    retry_token = resp_data.get("token")
                    logger.debug(f"Server requested retry with difficulty={new_diff}")
                    self._retry_token = retry_token
                    self._current_difficulty = new_diff

                    # Retry PoW with new difficulty
                    t0 = time.time()
                    pow_result = self._solve_pow(email, new_diff)
                    logger.debug(f"PoW solved (retry): difficulty={new_diff} ({time.time()-t0:.3f}s)")

                    payload["pow"] = pow_result
                    if retry_token:
                        payload["pow"]["token"] = retry_token

                    resp = await client.post("/api/accounts", json=payload)

            if resp.status_code != 201:
                raise EmailServiceError(
                    f"Account creation failed: {resp.status_code} {resp.text[:300]}"
                )

            account = resp.json()
            self.account_id = account["id"]
            self.auth_token = account["token"]
            self.password = password
            self.current_email = email
            self.email_created_at = datetime.now()

            duration = time.time() - start_time
            result = EmailResult(
                success=True,
                email=self.current_email,
                provider=EmailProvider.MAIL_TD.value,
                created_at=self.email_created_at,
                duration=duration,
                metadata={
                    "password": self.password,
                    "account_id": self.account_id,
                }
            )

            logger.info(f"Created new email: {self.current_email} ({duration:.2f}s)")
            return result

        except EmailServiceError:
            raise
        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"Failed to create email on mail.td: {str(e)}"
            logger.error(error_msg)

            return EmailResult(
                success=False,
                provider=EmailProvider.MAIL_TD.value,
                duration=duration,
                error=error_msg
            )

    async def check_verification_email(
        self,
        email: str,
        timeout: int = 300,
        check_interval: int = 5,
        browser_page: Optional[PlaywrightPage] = None,
    ) -> VerificationEmailResult:
        """
        Check for DigitalPlat verification email.

        Uses the mail.td HTTP API. The browser page is retained as an optional
        compatibility hook, but normal inbox polling does not depend on UI
        selectors or browser state.

        Args:
            email: Email address to check
            timeout: Maximum time to wait for email in seconds
            check_interval: Time between checks in seconds
            browser_page: Playwright page whose browser context will be used
                         to open a new tab for mail.td

        Returns:
            VerificationEmailResult with verification code if found
        """
        logger.info(f"Waiting for verification email at {email}")
        return await self._check_verification_by_api(email, timeout, check_interval)

    async def _check_verification_by_api(
        self,
        email: str,
        timeout: int = 300,
        check_interval: int = 5,
    ) -> VerificationEmailResult:
        """Check for verification email using mail.td HTTP API."""
        start_time = time.time()

        if not self.auth_token or not self.account_id:
            return VerificationEmailResult(
                found=False,
                duration=time.time() - start_time,
                error="No auth token available - call create_temporary_email first",
            )

        try:
            end_time = time.time() + timeout
            check_count = 0

            while time.time() < end_time:
                check_count += 1
                logger.debug(f"[API] Checking for verification email (attempt {check_count})")

                try:
                    messages = await self._fetch_messages()

                    if messages:
                        logger.debug(f"[API] Received {len(messages)} email(s)")
                        for msg in messages:
                            if not isinstance(msg, dict):
                                logger.debug(f"[API] Skipping non-dict message: {type(msg).__name__}")
                                continue
                            logger.info(
                                f"  Email: subject='{msg.get('subject', '(empty)')}' "
                                f"from='{self._sender_text(msg)}'"
                            )

                        for msg in messages:
                            if not isinstance(msg, dict):
                                continue
                            if self._is_verification_email(msg):
                                full_msg = await self._fetch_message_detail(msg["id"])
                                if not full_msg or not isinstance(full_msg, dict):
                                    full_msg = msg

                                logger.info(f"  verification email found via API: {full_msg.get('subject')}")

                                text_body = full_msg.get("text_body", "") or ""
                                html_body = full_msg.get("html_body", "") or ""
                                logger.info(f"  text_body length: {len(text_body)}")
                                logger.info(f"  text_body preview: {text_body[:1000]}")
                                if html_body:
                                    logger.info(f"  html_body length: {len(html_body)}")
                                    logger.info(f"  html_body preview: {html_body[:1000]}")

                                code = self._extract_code_from_message(full_msg)

                                if code:
                                    duration = time.time() - start_time
                                    result = VerificationEmailResult(
                                        found=True,
                                        code=code,
                                        received_at=datetime.now(),
                                        duration=duration,
                                        metadata={
                                            "subject": full_msg.get("subject", ""),
                                            "sender": self._sender_text(full_msg),
                                            "message_id": full_msg.get("id", ""),
                                            "source": "api",
                                        }
                                    )
                                    logger.info(f"Found verification code via API: {code} ({duration:.2f}s)")
                                    return result
                                else:
                                    logger.warning(
                                        f"API: Email matched but no code extracted from: {full_msg.get('subject', '')}"
                                    )
                                    logger.info(f"Full message JSON: {json.dumps(full_msg, indent=2, ensure_ascii=False)[:2000]}")
                    else:
                        logger.debug(f"  [API] No emails yet (check {check_count})")

                except Exception as e:
                    logger.debug(f"[API] Error checking emails: {str(e)}")

                if time.time() + check_interval < end_time:
                    await asyncio.sleep(check_interval)
                else:
                    break

            duration = time.time() - start_time
            logger.warning(f"[API] No verification email found in {timeout}s")

            try:
                final_messages = await self._fetch_messages()
                if final_messages:
                    logger.warning(f"[API] Found {len(final_messages)} email(s) but none matched:")
                    for msg in final_messages:
                        if isinstance(msg, dict):
                            logger.warning(f"  - '{msg.get('subject')}' from {msg.get('sender')}")
                        else:
                            logger.warning(f"  - (non-dict): {str(msg)[:100]}")
            except Exception:
                pass

            return VerificationEmailResult(
                found=False,
                duration=duration,
                error=f"API: Verification email not received within {timeout} seconds",
            )

        except Exception as e:
            duration = time.time() - start_time
            return VerificationEmailResult(
                found=False,
                duration=duration,
                error=f"API error: {str(e)}",
            )

    async def _check_verification_by_browser(
        self,
        email: str,
        timeout: int = 120,
        check_interval: int = 5,
        page: Optional[PlaywrightPage] = None,
    ) -> VerificationEmailResult:
        """
        Check for verification email by opening mail.td in a browser.

        Opens a NEW tab so it does not disrupt the current page (which may
        be showing the DigitalPlat verification popup). Logs into mail.td
        with the created email credentials, then polls the inbox for the
        DigitalPlat verification email.
        """
        start_time = time.time()

        if not page or not self.password:
            return VerificationEmailResult(
                found=False,
                duration=time.time() - start_time,
                error="Browser page or password not available for browser-based check",
            )

        # Get the browser context from the current page so we can create a new tab
        context = page.context
        if not context:
            return VerificationEmailResult(
                found=False,
                duration=time.time() - start_time,
                error="Cannot access browser context from page",
            )

        mailtd_page: Optional[PlaywrightPage] = None

        try:
            logger.info(f"[Browser] Opening mail.td in new tab to check verification email for {email}")

            # Open a new tab in the same context
            mailtd_page = await context.new_page()

            # Navigate to mail.td
            await mailtd_page.goto("https://mail.td/zh", wait_until="domcontentloaded")
            await asyncio.sleep(2)

            # Try to log in with our email credentials
            logged_in = await self._login_mailtd_browser(mailtd_page, email)
            if not logged_in:
                return VerificationEmailResult(
                    found=False,
                    duration=time.time() - start_time,
                    error="Failed to log into mail.td browser",
                )

            # Poll the inbox for verification emails
            end_time = time.time() + timeout
            check_count = 0

            while time.time() < end_time:
                check_count += 1
                remaining = end_time - time.time()

                logger.debug(f"[Browser] Polling inbox (attempt {check_count}, {remaining:.0f}s remaining)")

                try:
                    # Check inbox for messages on the page
                    code = await self._extract_code_from_browser_inbox(mailtd_page)
                    if code:
                        duration = time.time() - start_time
                        logger.info(f"[Browser] Found verification code: {code} ({duration:.2f}s)")
                        return VerificationEmailResult(
                            found=True,
                            code=code,
                            received_at=datetime.now(),
                            duration=duration,
                            metadata={"source": "browser"},
                        )

                    # Check if we need to refresh the inbox
                    await self._refresh_inbox(mailtd_page)

                except Exception as e:
                    logger.debug(f"[Browser] Error polling inbox: {str(e)}")

                if time.time() + check_interval < end_time:
                    await asyncio.sleep(check_interval)
                else:
                    break

            duration = time.time() - start_time
            return VerificationEmailResult(
                found=False,
                duration=duration,
                error=f"[Browser] No verification code found within {timeout}s",
            )

        except Exception as e:
            duration = time.time() - start_time
            return VerificationEmailResult(
                found=False,
                duration=duration,
                error=f"[Browser] Error: {str(e)}",
            )

        finally:
            # Always close the mail.td tab when done, to keep only the
            # DigitalPlat tab open for the verification popup handling step
            if mailtd_page:
                try:
                    await mailtd_page.close()
                    logger.debug("[Browser] Closed mail.td tab")
                except Exception:
                    pass

    async def _login_mailtd_browser(self, page: PlaywrightPage, email: str) -> bool:
        """Log into mail.td web UI using email and password.

        The flow is:
        1. Click "登录其他邮箱 →" (Login to other email) button
        2. Fill the 地址 (Address) textbox with the email
        3. Fill the 密码 (Password) textbox with the password
        4. Click "打开收件箱" (Open Inbox) button
        """
        try:
            # Check if already logged in (the email address shows in inbox)
            page_text = await page.inner_text("body")
            if email.lower() in page_text.lower():
                logger.debug("[Browser] Already logged into mail.td")
                return True

            # Step 1: Click "登录其他邮箱 →" to open the login modal
            login_trigger_selectors = [
                "button:has-text('登录其他邮箱')",
                "button:has-text('其他邮箱')",
            ]

            import_trigger_clicked = False
            for selector in login_trigger_selectors:
                try:
                    button = await page.query_selector(selector)
                    if button:
                        await button.click()
                        import_trigger_clicked = True
                        logger.debug("[Browser] Clicked 'Login to other email' button")
                        await asyncio.sleep(1)
                        break
                except Exception:
                    continue

            if not import_trigger_clicked:
                logger.warning("[Browser] Could not find 'Login to other email' button")
                return False

            # Step 2: Fill the 地址 (Address) textbox
            address_selectors: List[str] = [
                "input[placeholder*='地址']",
                "input[name='address']",
                "input[name='email']",
                "input[type='email']",
            ]

            address_filled = False
            for selector in address_selectors:
                try:
                    field = await page.query_selector(selector)
                    if field:
                        await field.fill(email)
                        address_filled = True
                        logger.debug(f"[Browser] Filled address field: {selector}")
                        break
                except Exception:
                    continue

            if not address_filled:
                logger.warning("[Browser] Could not find address input field")
                return False

            # Step 3: Fill the 密码 (Password) textbox
            password_selectors: List[str] = [
                "input[type='password']",
                "input[name='password']",
                "input[placeholder*='密码']",
            ]

            password_filled = False
            for selector in password_selectors:
                try:
                    field = await page.query_selector(selector)
                    if field:
                        await field.fill(self.password)
                        password_filled = True
                        logger.debug(f"[Browser] Filled password field: {selector}")
                        break
                except Exception:
                    continue

            if not password_filled:
                logger.warning("[Browser] Could not find password input field")
                return False

            # Step 4: Click "打开收件箱" (Open Inbox) button
            open_button_selectors = [
                "button:has-text('打开收件箱')",
                "button:has-text('打开')",
                "button[type='submit']",
            ]

            opened = False
            for selector in open_button_selectors:
                try:
                    button = await page.query_selector(selector)
                    if button:
                        await button.click()
                        opened = True
                        logger.debug("[Browser] Clicked 'Open Inbox' button")
                        await asyncio.sleep(3)
                        break
                except Exception:
                    continue

            if not opened:
                logger.warning("[Browser] Could not find 'Open Inbox' button")
                return False

            # Verify login succeeded: email address should now be visible in inbox
            await asyncio.sleep(1)
            page_text = await page.inner_text("body")
            if email.lower() in page_text.lower():
                logger.info("[Browser] Successfully logged into mail.td")
                return True

            logger.warning("[Browser] Could not verify mail.td login (email not found on page)")
            return False

        except Exception as e:
            logger.error(f"[Browser] Login error: {str(e)}")
            return False

    async def _extract_code_from_browser_inbox(self, page: PlaywrightPage) -> Optional[str]:
        """
        Extract verification code from mail.td inbox page.

        The mail.td page shows a list of emails in the inbox. Strategy:
        1. Read all visible text from the page (inbox shows email previews)
        2. Look for verification code patterns in the text
        3. If not found in preview, find and click an email to open full content
        """
        try:
            page_text = await page.inner_text("body")

            # Check if inbox has any emails (page shows "等待邮件" when empty)
            if "等待邮件" in page_text or "0" == await self._get_inbox_count(page):
                logger.debug("[Browser] Inbox is empty, no emails yet")
                return None

            # Inbox has emails - first check if the page already shows the code
            # (text content of email previews are visible on the inbox page)
            code = self._extract_code_from_text(page_text)
            if code:
                logger.info(f"[Browser] Found verification code on inbox page: {code}")
                return code

            # Code not found in preview - try clicking on an email to open it
            email_items = await self._find_email_items(page)
            if email_items:
                # Click the most recent email
                await email_items[0].click()
                await asyncio.sleep(2)

                # Read full email content
                email_content = await page.inner_text("body")
                logger.debug(f"[Browser] Opened email, content length: {len(email_content)}")
                logger.debug(f"[Browser] Email preview: {email_content[:1500]}")

                code = self._extract_code_from_text(email_content)
                if code:
                    logger.info(f"[Browser] Found verification code in opened email: {code}")
                    return code

                # Go back to inbox view (click the email address link at top)
                try:
                    back_link = await page.query_selector("a:has-text('@'), button:has-text('收件箱')")
                    if back_link:
                        await back_link.click()
                        await asyncio.sleep(1)
                except Exception:
                    pass

            return None

        except Exception as e:
            logger.debug(f"[Browser] Error extracting code from browser: {str(e)}")
            return None

    async def _get_inbox_count(self, page: PlaywrightPage) -> str:
        """Get the inbox email count from the page (shown next to heading)."""
        try:
            # The inbox heading shows count, e.g., "收件箱 3"
            page_text = await page.inner_text("body")
            match = re.search(r'收件箱\s*(\d+)', page_text)
            if match:
                return match.group(1)
        except Exception:
            pass
        return "0"

    async def _find_email_items(self, page: PlaywrightPage) -> list:
        """Find clickable email item elements on the inbox page."""
        # mail.td renders email items as divs in the inbox area.
        # Try multiple selectors to find them.
        selectors = [
            "[class*='list'] > div",
            "[class*='inbox'] > div",
            "[class*='email']:not([class*='address'])",
            ".item",
            "div[class*='row']",
            "div[role='button']",
        ]

        for selector in selectors:
            try:
                items = await page.query_selector_all(selector)
                if items:
                    return items
            except Exception:
                continue

        # Last resort: F5 to ensure we see fresh state, then snapshot-based
        return []

    async def _refresh_inbox(self, page: PlaywrightPage) -> None:
        """Refresh the inbox to check for new emails."""
        try:
            # mail.td has a "刷新" (Refresh) button next to the inbox heading
            refresh_selectors = [
                "button:has-text('刷新')",
                "[aria-label='refresh']",
                "[aria-label='刷新']",
                ".refresh-button",
            ]

            for selector in refresh_selectors:
                try:
                    btn = await page.query_selector(selector)
                    if btn:
                        await btn.click()
                        logger.debug("[Browser] Clicked refresh button")
                        await asyncio.sleep(2)
                        return
                except Exception:
                    continue

            # Fallback: press F5 to refresh
            try:
                await page.keyboard.press("F5")
                await asyncio.sleep(2)
            except Exception:
                pass

        except Exception as e:
            logger.debug(f"[Browser] Refresh error: {str(e)}")

    def _extract_code_from_text(self, text: str) -> Optional[str]:
        """
        Extract a verification code from arbitrary text.

        More aggressive version of _extract_code_from_message for raw page text.
        """
        if not text:
            return None

        # Pattern 1: Code near verification keywords
        patterns = [
            r'(?:verification code|verify code|code is|activation code)[:\s=]*["\']?(\d{4,8})',
            r'(?:验证码|认证码|识别码|校验码)[：:\s=]*(\d{4,8})',
            r'(?:your code|enter this code|input this code)[:\s=]*(\d{4,8})',
            r'(?:确认码|动态码|安全码)[：:\s]*(\d{4,8})',
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                return match.group(1)

        # Pattern 2: 6-digit code in a prominent position (bold, standalone, near sender)
        lines = text.split('\n')
        for i, line in enumerate(lines):
            # Look for lines that contain just a number or a number with minimal context
            numbers = re.findall(r'(\d{6})', line)
            if numbers:
                # Check surrounding context (sender line, subject)
                context = ""
                for j in range(max(0, i-3), min(len(lines), i+3)):
                    context += lines[j] + " "
                if any(kw in context.lower() for kw in ['digitalplat', 'verify', 'verification', 'code', '验证码', 'confirm']):
                    return numbers[0]

        # Pattern 3: Any 6+ digit number in the text
        all_numbers = re.findall(r'\b(\d{6,8})\b', text)
        if all_numbers:
            return all_numbers[0]

        return None

    async def wait_for_specific_sender(
        self,
        email: str,
        sender_pattern: str,
        timeout: int = 300,
        check_interval: int = 5
    ) -> VerificationEmailResult:
        """
        Wait for email from specific sender pattern.

        Args:
            email: Email address to monitor
            sender_pattern: Pattern to match sender (can be partial)
            timeout: Maximum time to wait in seconds
            check_interval: Time between checks in seconds

        Returns:
            VerificationEmailResult with the matched email
        """
        logger.info(f"Waiting for email from sender matching: {sender_pattern}")
        start_time = time.time()

        if not self.auth_token or not self.account_id:
            return VerificationEmailResult(
                found=False,
                duration=time.time() - start_time,
                error="Failed to obtain auth token for mail.td API",
            )

        try:
            end_time = time.time() + timeout

            while time.time() < end_time:
                try:
                    messages = await self._fetch_messages()

                    if messages:
                        for msg in messages:
                            if not isinstance(msg, dict):
                                continue
                            sender_text = self._sender_text(msg)

                            if sender_pattern.lower() in sender_text.lower():
                                full_msg = await self._fetch_message_detail(msg["id"])
                                if not full_msg:
                                    full_msg = msg

                                content = full_msg.get("text_body", "") or full_msg.get("html_body", "")
                                duration = time.time() - start_time

                                return VerificationEmailResult(
                                    found=True,
                                    sender=sender_text,
                                    content=content,
                                    received_at=datetime.now(),
                                    duration=duration,
                                    metadata={
                                        "subject": full_msg.get("subject", ""),
                                        "message_id": full_msg.get("id", ""),
                                    }
                                )

                except Exception as e:
                    logger.debug(f"Error checking for sender {sender_pattern}: {str(e)}")

                if time.time() + check_interval < end_time:
                    await asyncio.sleep(check_interval)
                else:
                    break

            duration = time.time() - start_time
            return VerificationEmailResult(
                found=False,
                duration=duration,
                error=f"No email from sender matching '{sender_pattern}' received within {timeout} seconds"
            )

        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"Error waiting for sender {sender_pattern}: {str(e)}"
            logger.error(error_msg)

            return VerificationEmailResult(
                found=False,
                duration=duration,
                error=error_msg
            )

    # --- HTTP API helpers ---

    async def _fetch_messages(self) -> List[Dict[str, Any]]:
        """
        Fetch messages for the current account using mail.td API.

        Returns:
            List of message summary dicts
        """
        if not self.auth_token or not self.account_id:
            return []

        try:
            client = await self._get_client()
            resp = await client.get(
                f"/api/accounts/{self.account_id}/messages?page=1",
                headers={"Authorization": f"Bearer {self.auth_token}"}
            )

            if resp.status_code != 200:
                logger.debug(f"Fetch messages error: {resp.status_code}")
                return []

            data = resp.json()
            return data.get("messages", [])
        except Exception as e:
            logger.debug(f"Error fetching messages: {str(e)}")
            return []

    async def _fetch_message_detail(self, message_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch individual message content using mail.td API.

        Returns:
            Message dict with text_body, html_body, and other fields.
        """
        if not self.auth_token or not self.account_id or not message_id:
            return None

        try:
            client = await self._get_client()
            resp = await client.get(
                f"/api/accounts/{self.account_id}/messages/{message_id}",
                headers={"Authorization": f"Bearer {self.auth_token}"}
            )

            if resp.status_code != 200:
                return None

            return resp.json()
        except Exception as e:
            logger.debug(f"Error fetching message detail: {str(e)}")
            return None

    # --- Code extraction helpers ---

    @staticmethod
    def _sender_text(message: Dict[str, Any]) -> str:
        """Normalize mail.td sender fields from current and legacy responses."""
        if not isinstance(message, dict):
            return ""

        sender = message.get("sender", "")
        if isinstance(sender, dict):
            sender_text = f"{sender.get('name', '')} {sender.get('address', '')}".strip()
        else:
            sender_text = str(sender).strip()

        from_address = str(message.get("from", "") or "").strip()
        if from_address and from_address.lower() not in sender_text.lower():
            sender_text = f"{sender_text} {from_address}".strip()
        return sender_text

    def _is_verification_email(self, message: Dict[str, Any]) -> bool:
        """Check if a message looks like a verification email"""
        if not isinstance(message, dict):
            return False

        # DigitalPlat-specific senders
        digitalplat_patterns = [
            "digitalplat",
            "@digitalplat.org",
            "@domain.digitalplat.org",
        ]
        generic_patterns = [
            "verification",
            "verify",
            "code",
            "验证码",
            "confirm",
            "activation",
            "welcome",
            "注册",
            "verify your",
        ]

        text = (
            f"{message.get('subject', '')} "
            f"{self._sender_text(message)}"
        ).lower()
        return (
            any(dp in text for dp in digitalplat_patterns)
            or any(gp in text for gp in generic_patterns)
        )

    def _extract_code_from_message(self, message: Dict[str, Any]) -> Optional[str]:
        """
        Extract verification code from message content.

        Checks text_body first, then html_body.
        """
        text = message.get("text_body", "") or ""
        html = message.get("html_body", "") or ""
        combined = f"{text}\n{html}"

        if not combined.strip():
            return None

        # Pattern 1: "code is XXXXXX" or "验证码：XXXXXX"
        code_patterns = [
            r'(?:code|验证码|Code|CODE|verification code|验证码)\s*[：:]\s*(\d{4,8})',
            r'(?:code|验证码|Code|CODE|verification code|验证码).*?(\d{6})',
            r'(\d{6,8}).*?(?:code|验证码|verification)',
            r'(?:enter|输入|use).*?(\d{4,8})',
        ]

        for pattern in code_patterns:
            match = re.search(pattern, combined, re.IGNORECASE | re.DOTALL)
            if match:
                return match.group(1)

        # Pattern 2: Look for standalone 6-digit codes near keywords
        context_pattern = r'(?:code|验证码|verify|verification|激活).*?(\d{6})(?!\d)'
        match = re.search(context_pattern, combined, re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1)

        # Pattern 3: Last resort - any 6-digit number in the email
        alt_pattern = r'\b(\d{6})\b'
        matches = re.findall(alt_pattern, combined)
        if matches:
            return matches[0]

        return None

    # --- Lifecycle ---

    async def close(self):
        """Close HTTP client"""
        if self.client and not self.client.is_closed:
            await self.client.aclose()
        self.client = None
        self._clear_auth_state()

    def _clear_auth_state(self):
        """Clear authentication state."""
        self.auth_token = None
        self.account_id = None
        self.password = None


class EmailServiceFactory:
    """Factory class for creating email service instances"""

    @staticmethod
    def create_service(config: EmailCredentials) -> EmailService:
        """
        Create appropriate email service based on configuration

        Args:
            config: Email service configuration

        Returns:
            EmailService instance

        Raises:
            EmailServiceError: If provider is not supported
        """
        if config.provider == EmailProvider.MAIL_TD:
            return MailTDService(config)
        elif config.provider == EmailProvider.TEN_MINUTE_MAIL:
            raise EmailServiceError("10minutemail not yet implemented")
        elif config.provider == EmailProvider.GUERRILLAR_MAIL:
            raise EmailServiceError("GuerrillaMail not yet implemented")
        else:
            raise EmailServiceError(f"Unsupported email provider: {config.provider}")
