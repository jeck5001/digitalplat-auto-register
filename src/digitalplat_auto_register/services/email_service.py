"""
Email Service Module

This module provides functionality for managing temporary email addresses
and retrieving verification codes from various email providers.
"""

import asyncio
import time
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple
from urllib.parse import quote

from loguru import logger
from playwright.async_api import async_playwright, Page, Browser, BrowserContext
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
    """
    
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
        
    async def create_temporary_email(self) -> EmailResult:
        """
        Create a new temporary email address using mail.td UI
        
        This method uses browser automation to:
        1. Navigate to mail.td homepage
        2. Click the "Create new email" button  
        3. Get a random email address
        4. Return the email details
        
        Returns:
            EmailResult with the created email details
            
        Raises:
            EmailServiceError: If email creation fails
            BrowserAutomationError: If browser automation fails
        """
        start_time = time.time()
        
        try:
            logger.info("Creating temporary email using mail.td")
            
            # Initialize browser
            await self._init_browser()
            
            # Navigate to mail.td
            await self.page.goto(self.base_url, wait_until="networkidle")
            logger.debug("Navigated to mail.td")
            
            new_mailbox = self.page.get_by_role(
                "button", name="New Mailbox", exact=True
            )
            await new_mailbox.wait_for(state="visible", timeout=15000)
            await new_mailbox.click()

            random_address = self.page.get_by_role(
                "button", name="Get Random Address", exact=True
            )
            await random_address.wait_for(state="visible", timeout=15000)
            for _ in range(40):
                if await random_address.is_enabled():
                    break
                await self.page.wait_for_timeout(250)
            else:
                raise EmailServiceError("mail.td did not enable random mailbox creation")

            logger.debug("Creating a random mailbox through the mail.td UI")
            await random_address.click()

            address_button = self.page.locator("button[title='Click to copy']")
            await address_button.wait_for(state="visible", timeout=15000)
            for _ in range(40):
                email = (await address_button.inner_text()).strip()
                if re.fullmatch(r"[^@\s]+@[^@\s]+", email):
                    break
                await self.page.wait_for_timeout(250)
            else:
                raise EmailServiceError("mail.td did not return a valid mailbox address")

            self.current_email = email
            self.email_created_at = datetime.now()
            mailbox_modal = self.page.locator("div.fixed.inset-0").filter(
                has_text="Your Email Address is Ready"
            )
            if await mailbox_modal.count():
                await mailbox_modal.locator("button").first.click()

            duration = time.time() - start_time
            result = EmailResult(
                success=True,
                email=self.current_email,
                provider=EmailProvider.MAIL_TD.value,
                created_at=self.email_created_at,
                duration=duration
            )

            logger.info(f"Created new email: {self.current_email}")
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
        Check for DigitalPlat verification email
        
        Args:
            email: Email address to check (username part only)
            timeout: Maximum time to wait for email in seconds
            check_interval: Time between checks in seconds
            
        Returns:
            VerificationEmailResult with verification code if found
        """
        logger.info(f"Waiting for verification email at {email}")
        start_time = time.time()
        
        # Ensure we're looking at the right inbox. Navigation failures used to
        # be swallowed, leaving ``self.page`` as None and causing every retry
        # to fail with ``NoneType.get_by_role``.
        if self.current_email != email or not await self._page_is_usable():
            ready = await self._navigate_to_email_inbox(email)
            if not ready:
                return VerificationEmailResult(
                    found=False,
                    duration=time.time() - start_time,
                    error="Temporary mailbox page is unavailable",
                )
        
        try:
            end_time = time.time() + timeout
            check_count = 0
            
            while time.time() < end_time:
                check_count += 1
                logger.debug(f"Checking for verification email (attempt {check_count})")
                
                try:
                    if not await self._page_is_usable():
                        if not await self._navigate_to_email_inbox(email):
                            return VerificationEmailResult(
                                found=False,
                                duration=time.time() - start_time,
                                error="Temporary mailbox page was closed or became unavailable",
                            )

                    if check_count > 1:
                        # Refresh within the authenticated mailbox session so newly
                        # delivered messages are loaded without changing mailboxes.
                        refresh_button = self.page.get_by_role(
                            "button", name="Refresh", exact=True
                        )
                        if await refresh_button.count():
                            await refresh_button.first.click()
                        else:
                            await self.page.reload(wait_until="domcontentloaded")
                        await self.page.wait_for_timeout(1000)

                    # Look for DigitalPlat verification email
                    email_element = await self._find_digitalplat_email()
                    
                    if email_element:
                        # Click to open the email
                        await email_element.click()
                        await asyncio.sleep(2)  # Allow email to load
                        
                        # Extract verification code
                        code = await self._extract_verification_code()
                        
                        if code:
                            duration = time.time() - start_time
                            result = VerificationEmailResult(
                                found=True,
                                code=code,
                                received_at=datetime.now(),
                                duration=duration
                            )
                            
                            logger.info("Found verification code in temporary mailbox")
                            return result
                        else:
                            logger.debug("Email found but no verification code detected")
                
                except Exception as e:
                    logger.debug(f"Error checking emails: {str(e)}")
                
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
        Wait for email from specific sender pattern
        
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
        
        try:
            end_time = time.time() + timeout
            
            while time.time() < end_time:
                try:
                    if not await self._page_is_usable():
                        if not await self._navigate_to_email_inbox(email):
                            return VerificationEmailResult(
                                found=False,
                                duration=time.time() - start_time,
                                error="Temporary mailbox page was closed or became unavailable",
                            )
                    # Refresh inbox
                    await self.page.reload(wait_until="domcontentloaded")
                    await asyncio.sleep(2)
                    
                    # Check all emails for sender match
                    emails = await self._get_all_emails()
                    
                    for email_elem in emails:
                        sender_text = await email_elem.text_content()
                        if sender_pattern.lower() in sender_text.lower():
                            # Found matching sender
                            await email_elem.click()
                            await asyncio.sleep(2)
                            
                            # Get email details
                            content = await self._get_email_content()
                            
                            duration = time.time() - start_time
                            
                            return VerificationEmailResult(
                                found=True,
                                sender=sender_text.strip(),
                                content=content,
                                received_at=datetime.now(),
                                duration=duration
                            )
                
                except Exception as e:
                    logger.debug(f"Error checking for sender {sender_pattern}: {str(e)}")
                
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
    
    async def _init_browser(self):
        """Initialize Playwright browser for mail.td interaction"""
        if not self.browser:
            self.playwright = await async_playwright().start()
            try:
                self.browser = await self.playwright.chromium.launch(headless=True)
            except Exception as exc:
                if "Executable doesn't exist" not in str(exc):
                    raise
                logger.info("Playwright Chromium is unavailable; using system Chrome for mail.td")
                self.browser = await self.playwright.chromium.launch(
                    channel="chrome", headless=True
                )
            self.context = await self.browser.new_context(viewport={'width': 1280, 'height': 720})
            self.page = await self.context.new_page()
    
    async def _page_is_usable(self) -> bool:
        if not self.page:
            return False
        try:
            return not self.page.is_closed()
        except Exception:
            return False

    async def _navigate_to_email_inbox(self, email: str) -> bool:
        """Navigate to specific email inbox"""
        try:
            if not await self._page_is_usable():
                await self._init_browser()
            if not self.page:
                return False
            encoded_email = quote(email, safe='')
            inbox_url = f"{self.base_url}/{encoded_email}"
            await self.page.goto(inbox_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            self.current_email = email
            return True
        except Exception as e:
            logger.debug(f"Error navigating to email inbox: {str(e)}")
            return False
    
    async def _find_digitalplat_email(self):
        """Find DigitalPlat verification email in inbox"""
        try:
            # These are Playwright text locators, not CSS selectors.
            text_patterns = [
                "DigitalPlat",
                "verification",
                "verify",
                "code",
                "验证码",
            ]
            
            for text in text_patterns:
                locator = self.page.get_by_text(text, exact=False).first
                if await locator.count():
                    return await locator.element_handle()
            
            return None
            
        except Exception as e:
            logger.debug(f"Error finding DigitalPlat email: {str(e)}")
            return None
    
    async def _extract_verification_code(self) -> Optional[str]:
        """Extract verification code from email content"""
        try:
            # mail.td's current UI renders the message panel with generated
            # class names and may place HTML email content in an iframe.
            contents = [await self.page.locator("body").inner_text()]
            for frame in self.page.frames:
                if frame != self.page.main_frame:
                    try:
                        contents.append(await frame.locator("body").inner_text())
                    except Exception:
                        continue
            content = "\n".join(contents)

            if content:
                # Look for 6-digit verification code
                code_pattern = r'(?:code|验证码|Code|CODE).*?(\d{6})'
                match = re.search(code_pattern, content, re.IGNORECASE | re.DOTALL)
                
                if match:
                    return match.group(1)
                
                # Alternative pattern - just look for 6-digit number
                alt_pattern = r'\b(\d{6})\b'
                matches = re.findall(alt_pattern, content)
                
                if matches:
                    return matches[0]  # Return first 6-digit number found
            
            return None
            
        except Exception as e:
            logger.debug(f"Error extracting verification code: {str(e)}")
            return None
    
    async def _get_all_emails(self):
        """Get all email elements in inbox"""
        try:
            selectors = [
                ".email-item",
                ".mail-item",
                "[class*='email']",
                "li",
                "div.email"
            ]
            
            for selector in selectors:
                elements = await self.page.query_selector_all(selector)
                if elements:
                    return elements
            
            return []
            
        except Exception as e:
            logger.debug(f"Error getting email elements: {str(e)}")
            return []
    
    async def _get_email_content(self) -> str:
        """Get current email content as text"""
        try:
            selectors = [
                ".email-content",
                ".message-body", 
                "[class*='content']",
                "[class*='body']"
            ]
            
            for selector in selectors:
                element = await self.page.query_selector(selector)
                if element:
                    return await element.text_content()
            
            return ""
            
        except Exception as e:
            logger.debug(f"Error getting email content: {str(e)}")
            return ""
    
    async def close(self):
        """Close browser resources"""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        self.browser = None
        self.context = None
        self.page = None
        self.playwright = None


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
        elif config.provider == EmailProvider.GUERRILLA_MAIL:
            raise EmailServiceError("GuerrillaMail not yet implemented")
        else:
            raise EmailServiceError(f"Unsupported email provider: {config.provider}")
