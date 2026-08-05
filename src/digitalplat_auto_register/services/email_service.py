"""
Email Service Module

This module provides functionality for managing temporary email addresses
and retrieving verification codes from various email providers.
"""

import asyncio
import time
import re
import json
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple
from urllib.parse import quote

from loguru import logger
from playwright.async_api import async_playwright, Page, Browser, BrowserContext

try:
    from camoufox.async_api import AsyncCamoufox
except ImportError:
    AsyncCamoufox = None

from bs4 import BeautifulSoup

from ..types import EmailCredentials, EmailProvider
from ..core.result import EmailResult, VerificationEmailResult
from ..exceptions import EmailServiceError, TimeoutError, BrowserAutomationError


class EmailService(ABC):
    """
    Abstract base class for email services
    """
    
    def __init__(self, config: EmailCredentials):
        """
        Initialize the email service
        
        Args:
            config: Email service configuration
        """
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
    mail.td temporary email service implementation
    
    This implementation uses browser automation to interact with the mail.td
    web interface since the service doesn't provide public APIs for automation.
    
    The new mail.td UI (2026-08):
    - Email address is auto-generated on page load
    - Credentials (email + password + token) are stored in localStorage
    - Messages are fetched via API: GET /api/accounts/{id}/messages
    - Individual message content: GET /api/accounts/{id}/messages/{msg_id}
    """
    
    # CSS selectors for the new mail.td UI
    EMAIL_ADDRESS_SELECTOR = "code[class*='CredentialCard_addr']"
    PASSWORD_SELECTOR = "button[class*='CredentialCard_pw']"
    CHANGE_EMAIL_BUTTON = "has-text('换一个')"
    REFRESH_BUTTON = "has-text('刷新')"
    
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
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        
        # Auth state
        self.auth_token: Optional[str] = None
        self.account_id: Optional[str] = None
        self.password: Optional[str] = None
    
    async def create_temporary_email(self) -> EmailResult:
        """
        Create a new temporary email address using mail.td UI.
        
        The new mail.td UI automatically generates an email on page load.
        We just need to extract the email address, password, and auth token.
        
        Returns:
            EmailResult with the created email details
            
        Raises:
            EmailServiceError: If email creation fails
            BrowserAutomationError: If browser automation fails
        """
        start_time = time.time()
        
        try:
            logger.info("Creating temporary email using mail.td (new UI)")
            
            # Initialize browser
            await self._init_browser()
            
            # Navigate to mail.td - email is auto-generated
            await self.page.goto(self.base_url, wait_until="domcontentloaded")
            logger.debug("Navigated to mail.td")
            # Wait briefly for the email address to render
            await asyncio.sleep(2)
            
            # Extract the auto-generated email address
            email = await self._extract_email_address()
            if not email:
                raise EmailServiceError("Failed to extract email address from mail.td")
            
            # Extract the password
            self.password = await self._extract_password()
            
            # Extract auth token and account ID from localStorage
            await self._extract_auth_state()
            
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
            
            logger.info(f"Created new email: {self.current_email} (account: {self.account_id})")
            return result
            
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
        check_interval: int = 5
    ) -> VerificationEmailResult:
        """
        Check for DigitalPlat verification email using mail.td API.
        
        Uses the stored auth token to call the messages API directly:
        - GET /api/accounts/{account_id}/messages?page=1
        - GET /api/accounts/{account_id}/messages/{message_id}
        
        Args:
            email: Email address to check (full email address)
            timeout: Maximum time to wait for email in seconds
            check_interval: Time between checks in seconds
            
        Returns:
            VerificationEmailResult with verification code if found
        """
        logger.info(f"Waiting for verification email at {email}")
        start_time = time.time()
        
        # If we don't have auth state, try to recover from the page
        if not self.auth_token or not self.account_id:
            if not await self._page_is_usable():
                ready = await self._navigate_to_inbox()
                if not ready:
                    return VerificationEmailResult(
                        found=False,
                        duration=time.time() - start_time,
                        error="Temporary mailbox page is unavailable",
                    )
            await self._extract_auth_state()
        
        if not self.auth_token or not self.account_id:
            return VerificationEmailResult(
                found=False,
                duration=time.time() - start_time,
                error="Failed to obtain auth token for mail.td API",
            )
        
        try:
            end_time = time.time() + timeout
            check_count = 0
            
            while time.time() < end_time:
                check_count += 1
                logger.debug(f"Checking for verification email (attempt {check_count})")
                
                try:
                    # Fetch messages via API
                    messages = await self._fetch_messages()
                    
                    if messages:
                        # Look for DigitalPlat verification email
                        for msg in messages:
                            if self._is_verification_email(msg):
                                # Get full message content
                                full_msg = await self._fetch_message_detail(msg["id"])
                                if not full_msg:
                                    full_msg = msg
                                
                                # Extract verification code
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
                                            "sender": full_msg.get("sender", {}).get("address", ""),
                                            "message_id": full_msg.get("id", ""),
                                        }
                                    )
                                    
                                    logger.info(f"Found verification code for {email}")
                                    return result
                                else:
                                    logger.debug(
                                        f"Email matched but no code found: {full_msg.get('subject', '')}"
                                    )
                    
                    # If this isn't the last iteration, refresh the inbox UI
                    # to trigger any lazy-loaded state and wait before retrying
                    if time.time() + check_interval < end_time:
                        await self._refresh_inbox()
                
                except Exception as e:
                    logger.debug(f"Error checking emails: {str(e)}")
                    # Try to recover the page session
                    if not await self._page_is_usable():
                        await self._navigate_to_inbox()
                
                # Wait before next check
                if time.time() + check_interval < end_time:
                    await asyncio.sleep(check_interval)
                else:
                    break
            
            # Timeout
            duration = time.time() - start_time
            error_msg = f"Verification email not received within {timeout} seconds"
            logger.error(error_msg)
            
            return VerificationEmailResult(
                found=False,
                duration=duration,
                error=error_msg
            )
            
        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"Error during email checking: {str(e)}"
            logger.error(error_msg)
            
            return VerificationEmailResult(
                found=False,
                duration=duration,
                error=error_msg
            )
    
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
            email: Email address to monitor (full email address)
            sender_pattern: Pattern to match sender (can be partial)
            timeout: Maximum time to wait in seconds
            check_interval: Time between checks in seconds
            
        Returns:
            VerificationEmailResult with the matched email
        """
        logger.info(f"Waiting for email from sender matching: {sender_pattern}")
        start_time = time.time()
        
        # If we don't have auth state, try to recover from the page
        if not self.auth_token or not self.account_id:
            if not await self._page_is_usable():
                ready = await self._navigate_to_inbox()
                if not ready:
                    return VerificationEmailResult(
                        found=False,
                        duration=time.time() - start_time,
                        error="Temporary mailbox page was closed or became unavailable",
                    )
            await self._extract_auth_state()
        
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
                    # Fetch messages via API
                    messages = await self._fetch_messages()
                    
                    if messages:
                        for msg in messages:
                            sender_info = msg.get("sender", {})
                            sender_address = sender_info.get("address", "")
                            sender_name = sender_info.get("name", "")
                            sender_text = f"{sender_name} {sender_address}".strip()
                            
                            if sender_pattern.lower() in sender_text.lower():
                                # Get full message content
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
                    if not await self._page_is_usable():
                        await self._navigate_to_inbox()
                
                # Wait before next check
                if time.time() + check_interval < end_time:
                    await asyncio.sleep(check_interval)
                else:
                    break
            
            # Timeout
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
    
    # --- Browser interaction helpers ---
    
    async def _init_browser(self):
        """Initialize Camoufox browser for mail.td interaction"""
        if not self.browser:
            self.playwright = await async_playwright().start()
            browser_args = {
                "headless": True,
            }
            camoufox = AsyncCamoufox(**browser_args)
            self.browser = await camoufox.start()
            self.context = await self.browser.new_context(
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
            )
            self.page = await self.context.new_page()
            await self.page.set_extra_http_headers({
                "Accept-Language": "en-US,en;q=0.9",
            })
    
    async def _page_is_usable(self) -> bool:
        """Check if the page is still open and usable"""
        if not self.page:
            return False
        try:
            return not self.page.is_closed()
        except Exception:
            return False
    
    async def _navigate_to_inbox(self) -> bool:
        """
        Navigate to mail.td main page to restore the inbox session.
        
        The new mail.td main page auto-logins if the auth token is in localStorage.
        """
        try:
            if not await self._page_is_usable():
                await self._init_browser()
            if not self.page:
                return False
            await self.page.goto(self.base_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2)
            return True
        except Exception as e:
            logger.debug(f"Error navigating to inbox: {str(e)}")
            return False
    
    async def _extract_email_address(self) -> Optional[str]:
        """Extract the email address from the mail.td UI"""
        try:
            # Wait for the email address element to appear
            email_element = self.page.locator(self.EMAIL_ADDRESS_SELECTOR).first
            await email_element.wait_for(state="visible", timeout=15000)
            
            for _ in range(50):
                text = (await email_element.text_content() or "").strip()
                if re.fullmatch(r"[^@\s]+@[^@\s]+", text):
                    return text
                await self.page.wait_for_timeout(250)
            
            return None
        except Exception as e:
            logger.debug(f"Error extracting email address: {str(e)}")
            return None
    
    async def _extract_password(self) -> Optional[str]:
        """Extract the password from the mail.td UI"""
        try:
            password_element = self.page.locator(self.PASSWORD_SELECTOR).first
            await password_element.wait_for(state="visible", timeout=10000)
            password = (await password_element.text_content() or "").strip()
            return password if password else None
        except Exception as e:
            logger.debug(f"Error extracting password: {str(e)}")
            return None
    
    async def _extract_auth_state(self):
        """Extract auth token and account ID from localStorage"""
        try:
            if not self.page:
                return
            self.auth_token = await self.page.evaluate(
                "localStorage.getItem('tempmail_token')"
            )
            self.account_id = await self.page.evaluate(
                "localStorage.getItem('tempmail_account_id')"
            )
            if self.auth_token:
                logger.debug(f"Auth token obtained (account: {self.account_id})")
        except Exception as e:
            logger.debug(f"Error extracting auth state: {str(e)}")
    
    async def _refresh_inbox(self):
        """Refresh the inbox by clicking the refresh button or reloading"""
        try:
            if not await self._page_is_usable():
                return
            # Try clicking the refresh button first
            refresh_button = self.page.get_by_role("button", name="Refresh", exact=True)
            if await refresh_button.count():
                await refresh_button.first.click()
            else:
                # Fall back to page reload
                await self.page.reload(wait_until="domcontentloaded")
            await self.page.wait_for_timeout(1000)
        except Exception as e:
            logger.debug(f"Error refreshing inbox: {str(e)}")
    
    # --- API helpers ---
    
    async def _fetch_messages(self) -> List[Dict[str, Any]]:
        """
        Fetch messages for the current account using mail.td API.
        
        Returns:
            List of message summary dicts (id, subject, sender, created_at, is_read)
        """
        if not self.auth_token or not self.account_id:
            return []
        
        try:
            if not self.page:
                return []
            
            result = await self.page.evaluate(
                """
                async ({accountId, token}) => {
                    try {
                        const resp = await fetch(`/api/accounts/${accountId}/messages?page=1`, {
                            credentials: 'include',
                            headers: {
                                'Authorization': `Bearer ${token}`,
                                'Content-Type': 'application/json'
                            }
                        });
                        if (!resp.ok) return null;
                        const data = await resp.json();
                        return data.messages || [];
                    } catch (e) {
                        return null;
                    }
                }
                """,
                {"accountId": self.account_id, "token": self.auth_token}
            )
            
            return result or []
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
            if not self.page:
                return None
            
            result = await self.page.evaluate(
                """
                async ({accountId, messageId, token}) => {
                    try {
                        const resp = await fetch(`/api/accounts/${accountId}/messages/${messageId}`, {
                            credentials: 'include',
                            headers: {
                                'Authorization': `Bearer ${token}`,
                                'Content-Type': 'application/json'
                            }
                        });
                        if (!resp.ok) return null;
                        return await resp.json();
                    } catch (e) {
                        return null;
                    }
                }
                """,
                {"accountId": self.account_id, "messageId": message_id, "token": self.auth_token}
            )
            
            return result
        except Exception as e:
            logger.debug(f"Error fetching message detail: {str(e)}")
            return None
    
    def _is_verification_email(self, message: Dict[str, Any]) -> bool:
        """Check if a message looks like a verification email"""
        patterns = [
            "verification",
            "verify",
            "code",
            "验证码",
            "confirm",
            "activation",
        ]
        text = (
            f"{message.get('subject', '')} "
            f"{message.get('sender', {}).get('name', '')} "
            f"{message.get('sender', {}).get('address', '')}"
        ).lower()
        return any(p.lower() in text for p in patterns)
    
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
    
    # --- Legacy UI interaction methods (kept for fallback) ---
    
    async def _click_change_email(self):
        """Click the '换一个' button to get a new random email"""
        try:
            button = self.page.get_by_role("button", name="↻ 换一个")
            if await button.count():
                await button.first.click()
                await asyncio.sleep(2)
                # Extract the new email
                new_email = await self._extract_email_address()
                if new_email:
                    self.current_email = new_email
                # Re-extract auth state (account ID changes)
                await self._extract_auth_state()
                logger.debug(f"Changed to new email: {self.current_email}")
        except Exception as e:
            logger.debug(f"Error changing email: {str(e)}")
    
    async def close(self):
        """Close browser resources"""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        self._clear_auth_state()
        self.browser = None
        self.context = None
        self.page = None
        self.playwright = None

    def _clear_auth_state(self):
        """Clear authentication state. Separated for testability."""
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
