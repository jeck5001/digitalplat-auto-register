"""
DigitalPlat Domain Registration Service

This module provides automated domain registration for DigitalPlat's free domains
using camoufox browser automation to bypass Cloudflare protection.

Supported TLDs: .dpdns.org, .us.kg, .xx.kg, .qzz.io, .qd.je
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse

from loguru import logger

try:
    from camoufox.async_api import AsyncCamoufox
except ImportError:
    AsyncCamoufox = None

from playwright.async_api import (
    async_playwright, Page, Browser, BrowserContext,
    TimeoutError as PlaywrightTimeoutError
)

from ..core.result import StepResult


@dataclass
class DomainRegistrationConfig:
    """Configuration for domain registration"""
    username: str
    password: str
    domain_prefix: str = ""
    domain_suffix: str = "dpdns.org"
    nameservers: List[str] = field(default_factory=lambda: [
        "ns1.cloudflare.com",
        "ns2.cloudflare.com"
    ])
    proxy: Optional[str] = None


@dataclass
class DomainCheckResult:
    """Result of domain availability check"""
    domain: str
    available: bool
    price: str = "Free"
    message: str = ""


@dataclass
class DomainRegistrationResult:
    """Result of domain registration attempt"""
    success: bool
    domain: str
    message: str = ""
    nameservers: List[str] = field(default_factory=list)
    registered_at: Optional[str] = None
    error: Optional[str] = None
    steps: List[Dict[str, Any]] = field(default_factory=list)


class DomainRegistrar:
    """
    DigitalPlat Domain Registration Service
    
    Uses camoufox browser automation to:
    1. Bypass Cloudflare protection
    2. Login to DigitalPlat dashboard
    3. Check domain availability
    4. Register domains
    """
    
    BASE_URL = "https://dash.domain.digitalplat.org"
    LOGIN_URL = f"{BASE_URL}/login"
    REGISTRATION_URL = f"{BASE_URL}/registration"
    
    def __init__(self, config: DomainRegistrationConfig):
        self.config = config
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._playwright = None
    
    async def _init_browser(self, headless: bool = True) -> None:
        """Initialize camoufox browser for stealth browsing"""
        if AsyncCamoufox is None:
            raise RuntimeError("camoufox not installed. Run: pip install camoufox")
        
        self._playwright = await async_playwright().start()
        
        # Launch camoufox with stealth options
        browser_args = {
            "headless": headless,
            "env": {
                "TZ": "America/New_York",
            },
        }
        
        if self.config.proxy:
            browser_args["proxy"] = {"server": self.config.proxy}
        
        # Use AsyncCamoufox for stealth
        camoufox = AsyncCamoufox(**browser_args)
        self.browser = await camoufox.start()
        
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
        )
        
        self.page = await self.context.new_page()
        
        # Set extra headers to appear more like a real browser
        await self.page.set_extra_http_headers({
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        })
        
        logger.info("Camoufox browser initialized for domain registration")
    
    async def _close_browser(self) -> None:
        """Close browser and cleanup"""
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser closed")
    
    async def _handle_cloudflare_challenge(self, timeout: int = 30) -> bool:
        """Wait for Cloudflare challenge to complete"""
        logger.info("Waiting for Cloudflare challenge...")
        start = time.time()
        
        while time.time() - start < timeout:
            title = await self.page.title()
            if "Just a moment" not in title and "请稍候" not in title:
                logger.info(f"Cloudflare bypassed. Page title: {title}")
                return True
            await asyncio.sleep(2)
        
        logger.warning("Cloudflare challenge timed out")
        return False
    
    async def _navigate_with_retry(self, url: str, max_retries: int = 3) -> bool:
        """Navigate to URL with Cloudflare handling and retries"""
        for attempt in range(max_retries):
            try:
                logger.info(f"Navigating to {url} (attempt {attempt + 1})")
                await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
                
                # Handle Cloudflare challenge
                if await self._handle_cloudflare_challenge():
                    return True
                
            except PlaywrightTimeoutError:
                logger.warning(f"Timeout navigating to {url}")
            except Exception as e:
                logger.error(f"Navigation error: {e}")
            
            await asyncio.sleep(3)
        
        return False
    
    async def login(self) -> StepResult:
        """Login to DigitalPlat domain dashboard"""
        start_time = time.time()
        
        try:
            if not await self._navigate_with_retry(self.LOGIN_URL):
                return StepResult(
                    name="navigate_to_login",
                    success=False,
                    duration=time.time() - start_time,
                    error="Failed to load login page (Cloudflare?)"
                )
            
            logger.info("Filling login form...")
            
            # Wait for and fill username field
            await self.page.wait_for_selector('input[name="username"], input[type="text"]', timeout=10000)
            username_field = await self.page.query_selector('input[name="username"], input[type="text"]')
            if username_field:
                await username_field.fill(self.config.username)
            
            # Fill password field
            await self.page.wait_for_selector('input[name="password"], input[type="password"]', timeout=5000)
            password_field = await self.page.query_selector('input[name="password"], input[type="password"]')
            if password_field:
                await password_field.fill(self.config.password)
            
            # Click login button
            login_btn = await self.page.query_selector('button[type="submit"], button:has-text("Login"), button:has-text("Sign in")')
            if login_btn:
                await login_btn.click()
            
            # Wait for navigation to complete
            await self.page.wait_for_load_state("networkidle", timeout=15000)
            
            # Verify login succeeded (check if we're no longer on login page)
            current_url = self.page.url
            if "/login" not in current_url:
                return StepResult(
                    name="login",
                    success=True,
                    duration=time.time() - start_time,
                    message=f"Logged in successfully as {self.config.username}"
                )
            else:
                return StepResult(
                    name="login",
                    success=False,
                    duration=time.time() - start_time,
                    error="Login failed - still on login page"
                )
                
        except Exception as e:
            return StepResult(
                name="login",
                success=False,
                duration=time.time() - start_time,
                error=str(e)
            )
    
    async def check_domain_availability(self, prefix: str = "", suffix: str = "") -> DomainCheckResult:
        """Check if a domain is available for registration"""
        prefix = prefix or self.config.domain_prefix
        suffix = suffix or self.config.domain_suffix
        domain = f"{prefix}.{suffix}"
        
        try:
            if not await self._navigate_with_retry(self.REGISTRATION_URL):
                return DomainCheckResult(
                    domain=domain,
                    available=False,
                    message="Failed to load registration page"
                )
            
            # Wait for the domain input field
            await self.page.wait_for_selector('input[name="domain"], input[placeholder*="domain"], input[placeholder*="Domain"]', timeout=10000)
            
            # Fill domain prefix
            domain_input = await self.page.query_selector(
                'input[name="domain"], input[placeholder*="domain"], input[placeholder*="Domain"]'
            )
            if domain_input:
                await domain_input.fill(prefix)
            
            # Check if suffix selector exists and select it
            suffix_select = await self.page.query_selector('select[name="suffix"], select[name="tld"]')
            if suffix_select:
                await suffix_select.select_option(f".{suffix}")
            
            # Click check button
            check_btn = await self.page.query_selector(
                'button:has-text("Check"), button:has-text("check"), input[type="submit"]'
            )
            if check_btn:
                await check_btn.click()
            
            # Wait for result
            await self.page.wait_for_load_state("networkidle", timeout=10000)
            
            # Check page content for availability indication
            page_content = await self.page.content()
            
            # Look for positive indicators
            available_indicators = ["available", "Available", "可注册", "is available"]
            unavailable_indicators = ["taken", "unavailable", "not available", "已被注册", "already registered"]
            
            is_available = any(indicator in page_content for indicator in available_indicators)
            is_unavailable = any(indicator in page_content for indicator in unavailable_indicators)
            
            if is_available and not is_unavailable:
                return DomainCheckResult(domain=domain, available=True, message="Domain is available!")
            elif is_unavailable:
                return DomainCheckResult(domain=domain, available=False, message="Domain is already taken")
            else:
                # Try to determine from URL or other signals
                return DomainCheckResult(
                    domain=domain, 
                    available=False, 
                    message="Could not determine availability"
                )
                
        except Exception as e:
            return DomainCheckResult(
                domain=domain,
                available=False,
                message=f"Error checking domain: {e}"
            )
    
    async def register_domain(self, prefix: str = "", suffix: str = "") -> DomainRegistrationResult:
        """Register a domain"""
        prefix = prefix or self.config.domain_prefix
        suffix = suffix or self.config.domain_suffix
        domain = f"{prefix}.{suffix}"
        
        result = DomainRegistrationResult(success=False, domain=domain)
        start_time = time.time()
        
        try:
            # Initialize browser
            await self._init_browser(headless=True)
            
            # Step 1: Login
            result.steps.append({"name": "init_browser", "success": True, "message": "Browser initialized"})
            
            login_result = await self.login()
            result.steps.append({
                "name": "login",
                "success": login_result.success,
                "message": login_result.message or login_result.error
            })
            
            if not login_result.success:
                result.error = f"Login failed: {login_result.error}"
                return result
            
            # Step 2: Navigate to registration
            if not await self._navigate_with_retry(self.REGISTRATION_URL):
                result.error = "Failed to navigate to registration page"
                return result
            
            result.steps.append({"name": "navigate", "success": True, "message": "On registration page"})
            
            # Step 3: Fill domain name
            await self.page.wait_for_selector(
                'input[name="domain"], input[placeholder*="domain"], input[placeholder*="Domain"]',
                timeout=10000
            )
            
            domain_input = await self.page.query_selector(
                'input[name="domain"], input[placeholder*="domain"], input[placeholder*="Domain"]'
            )
            if domain_input:
                await domain_input.fill(prefix)
            
            # Select suffix if available
            suffix_select = await self.page.query_selector('select[name="suffix"], select[name="tld"]')
            if suffix_select:
                await suffix_select.select_option(f".{suffix}")
            
            # Step 4: Check availability
            check_btn = await self.page.query_selector(
                'button:has-text("Check"), button:has-text("check")'
            )
            if check_btn:
                await check_btn.click()
                await self.page.wait_for_load_state("networkidle", timeout=10000)
            
            result.steps.append({"name": "check_domain", "success": True, "message": "Checked availability"})
            
            # Step 5: Fill nameservers if needed
            for i, ns in enumerate(self.config.nameservers[:2]):
                ns_field = await self.page.query_selector(
                    f'input[name="nameserver{i+1}"], input[name="ns{i+1}"], '
                    f'input[placeholder*="nameserver{i+1}"], input[placeholder*="NS{i+1}"]'
                )
                if ns_field:
                    await ns_field.fill(ns)
            
            # Step 6: Accept terms if checkbox exists
            terms_checkbox = await self.page.query_selector(
                'input[name="terms"], input[name="agree"], input[type="checkbox"]'
            )
            if terms_checkbox:
                await terms_checkbox.check()
            
            # Step 7: Submit registration
            submit_btn = await self.page.query_selector(
                'button:has-text("Register"), button:has-text("注册"), button[type="submit"]'
            )
            if submit_btn:
                await submit_btn.click()
                await self.page.wait_for_load_state("networkidle", timeout=15000)
            
            # Step 8: Verify registration success
            page_content = await self.page.content()
            success_indicators = ["success", "Success", "successful", "registered", "注册成功", "成功"]
            
            if any(indicator in page_content for indicator in success_indicators):
                result.success = True
                result.message = f"Domain {domain} registered successfully!"
                result.registered_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                result.nameservers = self.config.nameservers
                result.steps.append({"name": "register", "success": True, "message": "Registration confirmed"})
            else:
                # Check for error messages
                result.message = "Registration submitted but success not confirmed"
                result.steps.append({"name": "register", "success": True, "message": "Submitted, verification needed"})
            
            return result
                
        except Exception as e:
            result.error = str(e)
            result.steps.append({"name": "error", "success": False, "message": str(e)})
            return result
        
        finally:
            await self._close_browser()


async def register_domain_with_defaults(
    username: str,
    password: str,
    domain_prefix: str,
    domain_suffix: str = "dpdns.org",
    nameservers: Optional[List[str]] = None,
    proxy: Optional[str] = None,
) -> DomainRegistrationResult:
    """
    Convenience function to register a domain with default settings
    
    Args:
        username: DigitalPlat username
        password: DigitalPlat password
        domain_prefix: Domain prefix (without TLD)
        domain_suffix: Domain TLD (default: dpdns.org)
        nameservers: Optional list of nameservers
        proxy: Optional proxy URL
        
    Returns:
        DomainRegistrationResult with registration status
    """
    config = DomainRegistrationConfig(
        username=username,
        password=password,
        domain_prefix=domain_prefix,
        domain_suffix=domain_suffix,
        nameservers=nameservers or ["ns1.cloudflare.com", "ns2.cloudflare.com"],
        proxy=proxy,
    )
    
    registrar = DomainRegistrar(config)
    return await registrar.register_domain()
