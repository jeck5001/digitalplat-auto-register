"""
Browser Automation Service

This module provides Playwright-based browser automation for DigitalPlat
registration process including form filling, submission, and verification popup handling.
"""

import asyncio
import time
from datetime import datetime
from typing import Optional, Dict, Any, Tuple, List
from dataclasses import dataclass
from urllib.parse import urlparse

from playwright.async_api import (
    async_playwright, Page, Browser, BrowserContext, 
    TimeoutError as PlaywrightTimeoutError
)
try:
    from camoufox.async_api import AsyncCamoufox
except ImportError:
    AsyncCamoufox = None
from loguru import logger
from fake_useragent import UserAgent

from ..types import BrowserConfig, ProxyConfig, UserProfile
from ..core.result import BrowserResult
from ..exceptions import BrowserAutomationError, TimeoutError


@dataclass
class FormField:
    """Represents a form field with its metadata"""
    name: str
    selector: str
    field_type: str = "text"  # text, password, email, tel, etc.
    required: bool = True
    placeholder: str = ""
    value: str = ""


class BrowserAutomationService:
    """
    Playwright-based browser automation service for DigitalPlat registration
    
    Provides functionality for:
    - Browser initialization and management
    - Navigation to registration pages
    - Form filling with user data
    - Turnstile token injection
    - Form submission and verification popup handling
    - Screenshot and debugging utilities
    """
    
    def __init__(self, browser_config: BrowserConfig, proxy_config: Optional[ProxyConfig] = None):
        """
        Initialize the browser automation service
        
        Args:
            browser_config: Browser configuration
            proxy_config: Proxy configuration (optional)
        """
        self.browser_config = browser_config
        self.proxy_config = proxy_config
        self.ua = UserAgent()
        
        # Browser resources
        self.playwright = None
        self.camoufox = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        
        # State tracking
        self.current_url: Optional[str] = None
        self.screenshots_taken: List[str] = []
        
        logger.debug("BrowserAutomationService initialized")
    
    async def initialize(self):
        """Initialize browser resources"""
        if not self.browser:
            logger.info("Initializing browser...")

            engine = self.browser_config.engine.lower()
            if engine not in {"chromium", "camoufox"}:
                raise BrowserAutomationError(f"Unsupported browser engine: {engine}")

            proxy = None
            if self.proxy_config and self.proxy_config.enabled:
                proxy = {
                    "server": self.proxy_config.server
                }
                
                if self.proxy_config.username and self.proxy_config.password:
                    proxy["username"] = self.proxy_config.username
                    proxy["password"] = self.proxy_config.password

            if engine == "camoufox":
                if AsyncCamoufox is None:
                    raise BrowserAutomationError(
                        "Camoufox is not installed. Install the camoufox package and browser binary."
                    )
                self.camoufox = AsyncCamoufox(headless=self.browser_config.headless)
                self.browser = await self.camoufox.start()
                # Camoufox manages a consistent fingerprint and user agent itself.
                context_options = {
                    "no_viewport": True,
                    "accept_downloads": self.browser_config.accept_downloads,
                    "ignore_https_errors": self.browser_config.accept_insecure_certs,
                }
            else:
                self.playwright = await async_playwright().start()
                launch_options = {
                    "headless": self.browser_config.headless,
                    "timeout": self.browser_config.timeout,
                }
                if proxy:
                    launch_options["proxy"] = proxy
                self.browser = await self.playwright.chromium.launch(**launch_options)
                context_options = {
                    "viewport": {
                        "width": self.browser_config.viewport_width,
                        "height": self.browser_config.viewport_height,
                    },
                    "accept_downloads": self.browser_config.accept_downloads,
                    "ignore_https_errors": self.browser_config.accept_insecure_certs,
                }
                context_options["user_agent"] = self.browser_config.user_agent or self.ua.chrome

            if proxy and engine == "camoufox":
                context_options["proxy"] = proxy
            self.context = await self.browser.new_context(**context_options)
            
            # Create page
            self.page = await self.context.new_page()
            
            # Set default timeout
            self.page.set_default_timeout(self.browser_config.timeout)
            
            logger.info("Browser initialized successfully")
    
    async def navigate_to_registration(self, base_url: str, referral_code: str = "") -> BrowserResult:
        """
        Navigate to the DigitalPlat registration page
        
        Args:
            base_url: Base URL of DigitalPlat
            referral_code: Referral code to use
            
        Returns:
            BrowserResult with navigation status
        """
        start_time = time.time()
        
        try:
            logger.info(f"Navigating to registration page: {base_url}")
            
            # Build registration URL
            if referral_code:
                url = f"{base_url.rstrip('/')}/auth/register?ref={referral_code}"
            else:
                url = f"{base_url.rstrip('/')}/auth/register"
            
            # Cloudflare challenge pages keep network activity open, so wait for
            # the actual registration field rather than a network-idle state.
            await self.page.goto(url, wait_until="domcontentloaded")
            await self.page.locator("input[name='username']").wait_for(
                state="visible", timeout=max(self.browser_config.timeout, 60000)
            )
            
            self.current_url = url
            
            # Take screenshot
            screenshot_path = await self.take_screenshot("registration_page")
            
            duration = time.time() - start_time
            result = BrowserResult(
                success=True,
                url=url,
                title=await self.page.title(),
                screenshot=screenshot_path,
                duration=duration
            )
            
            logger.info(f"Successfully navigated to registration page: {result.title}")
            return result
            
        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"Failed to navigate to registration page: {str(e)}"
            logger.error(error_msg)
            
            return BrowserResult(
                success=False,
                url=url if 'url' in locals() else None,
                duration=duration,
                error=error_msg
            )
    
    async def fill_registration_form(
        self, 
        user_profile: UserProfile, 
        turnstile_token: str
    ) -> BrowserResult:
        """
        Fill the registration form with user data and Turnstile token
        
        Args:
            user_profile: User profile information
            turnstile_token: Valid Cloudflare Turnstile token
            
        Returns:
            BrowserResult with form filling status
        """
        start_time = time.time()
        
        try:
            logger.info("Filling registration form...")
            
            # Define form fields
            form_fields = [
                FormField("username", "input[name='username']", "text", placeholder="Username"),
                FormField("email", "input[name='email']", "email", placeholder="Email Address"),
                FormField("fullname", "input[name='fullname']", "text", placeholder="Full Name"),
                FormField("phone", "input[name='phone']", "tel", placeholder="Phone Number"),
                FormField("password", "input[name='password']", "password", placeholder="Password"),
                FormField("referral_code", "input[name*='referral']", "text", required=False),
                FormField("address_line1", "input[placeholder='Address Line 1']", "text"),
                FormField("address_line2", "input[placeholder='Address Line 2 (optional)']", "text", required=False),
                FormField("city", "input[placeholder='City']", "text"),
                FormField("state", "input[placeholder='State / Province / Region']", "text"),
                FormField("postal_code", "input[placeholder='Postal Code']", "text"),
                FormField("country", "select", "select"),
            ]
            
            # Fill each field
            for field in form_fields:
                try:
                    await self._fill_form_field(field, user_profile)
                except Exception as e:
                    logger.warning(f"Failed to fill field {field.name}: {str(e)}")
                    if field.required:
                        raise BrowserAutomationError(f"Required field {field.name} could not be filled: {str(e)}")

            # The registration form requires a billing address. This client only
            # collects WHOIS fields, so explicitly use them for billing as well.
            await self._use_whois_address_for_billing()
            
            # Inject Turnstile token
            await self._inject_turnstile_token(turnstile_token)
            
            # Take screenshot
            screenshot_path = await self.take_screenshot("form_filled")
            
            duration = time.time() - start_time
            result = BrowserResult(
                success=True,
                url=self.current_url,
                screenshot=screenshot_path,
                duration=duration
            )
            
            logger.info("Registration form filled successfully")
            return result
            
        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"Failed to fill registration form: {str(e)}"
            logger.error(error_msg)
            
            return BrowserResult(
                success=False,
                url=self.current_url,
                duration=duration,
                error=error_msg
            )
    
    async def submit_registration_form(self) -> BrowserResult:
        """
        Submit the registration form and handle resulting popups
        
        Returns:
            BrowserResult with submission status
        """
        start_time = time.time()
        
        try:
            logger.info("Submitting registration form...")
            
            # Look for submit button
            submit_selectors = [
                "button[type='submit']",
                "button:has-text('Register')",
                "button:has-text('Sign Up')",
                "button:has-text('Create Account')",
                "input[type='submit']"
            ]
            
            submit_button = None
            for selector in submit_selectors:
                button = await self.page.query_selector(selector)
                if button:
                    submit_button = button
                    break
            
            if not submit_button:
                raise BrowserAutomationError("No submit button found")
            
            if not await submit_button.is_enabled():
                raise BrowserAutomationError(
                    "Registration submit button is disabled after form filling; "
                    "verify the contact fields and Turnstile token"
                )

            current_origin = urlparse(self.page.url)

            def is_registration_response(response) -> bool:
                parsed_url = urlparse(response.url)
                return (
                    response.request.method == "POST"
                    and parsed_url.scheme == current_origin.scheme
                    and parsed_url.netloc == current_origin.netloc
                )

            response_future = None
            try:
                async with self.page.expect_response(
                    is_registration_response,
                    timeout=min(self.browser_config.timeout, 10000),
                ) as response_info:
                    await submit_button.click()
                response_future = await response_info.value
            except PlaywrightTimeoutError:
                # The verification UI check below provides the final outcome if
                # the app does not make an observable same-origin request.
                pass

            if response_future and response_future.status >= 400:
                error_detail = await self._get_response_error_text(response_future)
                detail = f": {error_detail}" if error_detail else ""
                raise BrowserAutomationError(
                    f"Registration request failed with HTTP {response_future.status}" + detail
                )
            
            # Wait for response (new page, popup, etc.)
            await asyncio.sleep(3)
            
            # Check for verification popup
            verification_started = await self._wait_for_verification_popup()
            if not verification_started:
                error_text = await self._get_submission_error_text()
                detail = f": {error_text}" if error_text else ""
                raise BrowserAutomationError(
                    "Registration was not accepted; no email verification step appeared" + detail
                )
            
            # Take screenshot
            screenshot_path = await self.take_screenshot("form_submitted")
            
            duration = time.time() - start_time
            
            # Get current page info
            try:
                title = await self.page.title()
                url = self.page.url
            except:
                title = "Unknown"
                url = self.current_url
            
            result = BrowserResult(
                success=True,
                url=url,
                title=title,
                screenshot=screenshot_path,
                duration=duration
            )
            
            logger.info("Registration form submitted successfully")
            return result
            
        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"Failed to submit registration form: {str(e)}"
            logger.error(error_msg)
            
            return BrowserResult(
                success=False,
                url=self.current_url,
                duration=duration,
                error=error_msg
            )
    
    async def handle_verification_popup(self, verification_code: str) -> BrowserResult:
        """
        Handle email verification popup by entering code and submitting
        
        Args:
            verification_code: Verification code from email
            
        Returns:
            BrowserResult with verification status
        """
        start_time = time.time()
        
        try:
            logger.info("Handling verification popup...")
            
            # Find the verification popup
            popup = await self._find_verification_popup()
            if not popup:
                raise BrowserAutomationError("Verification popup not found")
            
            # Find verification code input
            code_input_selectors = [
                "input[placeholder*='code']",
                "input[placeholder*='验证码']",
                "input[placeholder*='Enter code']",
                "input[type='text']",
                "input:not([type])"
            ]
            
            code_input = None
            for selector in code_input_selectors:
                input_elem = await popup.query_selector(selector)
                if input_elem:
                    code_input = input_elem
                    break
            
            if not code_input:
                raise BrowserAutomationError("Verification code input not found in popup")
            
            # Fill verification code
            await code_input.fill(verification_code)
            
            # Find and click verify button
            verify_button_selectors = [
                "button:has-text('Verify')",
                "button:has-text('验证')",
                "button:has-text('确认')",
                "button:has-text('Submit')"
            ]
            
            verify_button = None
            for selector in verify_button_selectors:
                button = await popup.query_selector(selector)
                if button:
                    verify_button = button
                    break
            
            if not verify_button:
                raise BrowserAutomationError("Verify button not found in popup")
            
            # Check if button is enabled before clicking
            is_enabled = await verify_button.is_enabled()
            if not is_enabled:
                logger.warning("Verify button is initially disabled, code might need to be validated")
                await asyncio.sleep(1)  # Wait for validation
                is_enabled = await verify_button.is_enabled()
            
            if is_enabled:
                await verify_button.click()
            else:
                raise BrowserAutomationError("Verify button is disabled after entering code")
            
            # Wait for verification to complete
            await asyncio.sleep(3)
            
            # Take screenshot
            screenshot_path = await self.take_screenshot("verification_completed")
            
            duration = time.time() - start_time
            result = BrowserResult(
                success=True,
                url=self.page.url,
                title=await self.page.title(),
                screenshot=screenshot_path,
                duration=duration
            )
            
            logger.info("Verification popup handled successfully")
            return result
            
        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"Failed to handle verification popup: {str(e)}"
            logger.error(error_msg)
            
            return BrowserResult(
                success=False,
                url=self.current_url,
                duration=duration,
                error=error_msg
            )
    
    async def _fill_form_field(self, field: FormField, user_profile: UserProfile) -> None:
        """Fill a single form field with appropriate value"""
        # Map field names to user profile attributes
        field_value_map = {
            "username": user_profile.username,
            "email": user_profile.email,
            "fullname": user_profile.fullname,
            "phone": user_profile.phone,
            "password": user_profile.password,
            "referral_code": user_profile.referral_code,
            "address_line1": user_profile.address_line1,
            "address_line2": user_profile.address_line2,
            "city": user_profile.city,
            "state": user_profile.state,
            "postal_code": user_profile.postal_code,
            "country": user_profile.country,
        }
        
        value = field_value_map.get(field.name, "")
        
        if not value and field.required:
            raise BrowserAutomationError(f"Required value missing for field: {field.name}")
        
        if value:
            try:
                element = await self.page.wait_for_selector(field.selector, timeout=5000)
                if field.field_type == "select":
                    await element.select_option(value)
                else:
                    await element.fill(value)
                logger.debug(f"Filled field {field.name}")
            except PlaywrightTimeoutError:
                # Try alternative selectors
                alternative_selectors = self._get_alternative_selectors(field.name)
                element = None
                
                for alt_selector in alternative_selectors:
                    try:
                        element = await self.page.query_selector(alt_selector)
                        if element:
                            break
                    except:
                        continue
                
                if element:
                    if field.field_type == "select":
                        await element.select_option(value)
                    else:
                        await element.fill(value)
                    logger.debug(f"Filled field {field.name} with alternative selector")
                else:
                    if field.required:
                        raise BrowserAutomationError(f"Could not find element for required field: {field.name}")
                    else:
                        logger.debug(f"Optional field {field.name} not found, skipping")

    async def _use_whois_address_for_billing(self) -> None:
        """Check the form option that reuses WHOIS data for billing."""
        selector = "label:has-text('Billing address is the same as WHOIS') input[type='checkbox']"
        checkbox = self.page.locator(selector)
        if await checkbox.count() == 0:
            raise BrowserAutomationError("Billing address confirmation checkbox not found")

        checkbox = checkbox.first
        if not await checkbox.is_checked():
            await checkbox.check()
        logger.debug("Set billing address to reuse WHOIS address")
    
    def _get_alternative_selectors(self, field_name: str) -> List[str]:
        """Get alternative CSS selectors for a field name"""
        selector_map = {
            "username": [
                "input[name*='user']",
                "input[id*='user']",
                "input[placeholder*='user']",
                "input[aria-label*='user']"
            ],
            "email": [
                "input[name*='mail']", 
                "input[id*='mail']",
                "input[placeholder*='mail']",
                "input[type='email']"
            ],
            "fullname": [
                "input[name*='name']",
                "input[id*='name']", 
                "input[placeholder*='name']",
                "input[placeholder*='full']"
            ],
            "phone": [
                "input[name*='phone']",
                "input[id*='phone']",
                "input[placeholder*='phone']",
                "input[type='tel']"
            ],
            "password": [
                "input[name*='pass']",
                "input[id*='pass']",
                "input[placeholder*='pass']",
                "input[type='password']"
            ]
        }
        return selector_map.get(field_name, [])
    
    async def _inject_turnstile_token(self, token: str) -> None:
        """Inject Turnstile token into the page"""
        try:
            response_selector = "input[name='cf-turnstile-response']"
            await self.page.locator(response_selector).wait_for(
                state="attached",
                timeout=min(self.browser_config.timeout, 15000),
            )
            injection_result = await self.page.evaluate(
                """
                (token) => {
                    const setInputValue = (input) => {
                        const setter = Object.getOwnPropertyDescriptor(
                            HTMLInputElement.prototype, "value"
                        )?.set;
                        if (setter) {
                            setter.call(input, token);
                        } else {
                            input.value = token;
                        }
                        input.dispatchEvent(new Event("input", { bubbles: true }));
                        input.dispatchEvent(new Event("change", { bubbles: true }));
                    };

                    const responseInputs = [
                        ...document.querySelectorAll(
                            "input[data-cf-turnstile], input[name='cf-turnstile-response']"
                        ),
                    ];
                    responseInputs.forEach(setInputValue);
                    window.cf_turnstile_response = token;

                    const callbackNames = [
                        "onSuccess",
                        "onVerify",
                        "onToken",
                        "onChange",
                        "callback",
                    ];
                    // The registration page keeps the token in React state.
                    // Prefer the mounted Turnstile callback over only mutating
                    // the hidden response input.
                    let reactCallbackInvoked = false;
                    let reactCallbackName = null;
                    const invokeReactCallback = (props) => {
                        if (!props || !(props.siteKey || props.sitekey)) {
                            return false;
                        }
                        const callbackName = callbackNames.find((name) =>
                            typeof props[name] === "function"
                        );
                        if (!callbackName) {
                            return false;
                        }
                        props[callbackName](token);
                        reactCallbackName = callbackName;
                        return true;
                    };
                    for (const input of responseInputs) {
                        for (let element = input; element && !reactCallbackInvoked; element = element.parentElement) {
                            const fiberKey = Object.keys(element).find((key) =>
                                key.startsWith("__reactFiber$")
                            );
                            let fiber = fiberKey ? element[fiberKey] : null;
                            for (let depth = 0; fiber && depth < 100; depth += 1, fiber = fiber.return) {
                                const props = fiber.memoizedProps;
                                if (invokeReactCallback(props)) {
                                    reactCallbackInvoked = true;
                                    break;
                                }
                            }
                        }
                    }

                    if (!reactCallbackInvoked) {
                        const roots = new Set();
                        const hook = window.__REACT_DEVTOOLS_GLOBAL_HOOK__;
                        if (hook?.renderers && typeof hook.getFiberRoots === "function") {
                            for (const rendererId of hook.renderers.keys()) {
                                for (const root of hook.getFiberRoots(rendererId)) {
                                    roots.add(root.current || root);
                                }
                            }
                        }
                        for (const element of document.querySelectorAll("*")) {
                            const fiberKey = Object.keys(element).find((key) =>
                                key.startsWith("__reactFiber$")
                            );
                            let fiber = fiberKey ? element[fiberKey] : null;
                            while (fiber?.return) {
                                fiber = fiber.return;
                            }
                            if (fiber) {
                                roots.add(fiber);
                            }
                        }

                        const visited = new Set();
                        const invokeRegistrationStateSetter = (fiber) => {
                            const hooks = [];
                            for (
                                let hook = fiber.memoizedState;
                                hook && hooks.length < 40;
                                hook = hook.next
                            ) {
                                hooks.push(hook);
                            }
                            for (let start = 0; start <= hooks.length - 10; start += 1) {
                                const states = hooks.slice(start, start + 10).map(
                                    (hook) => hook.memoizedState
                                );
                                const isRegistrationState =
                                    typeof states[0] === "boolean" &&
                                    typeof states[1] === "string" &&
                                    typeof states[2] === "number" &&
                                    typeof states[3] === "string" &&
                                    typeof states[5] === "string" &&
                                    typeof states[6] === "number" &&
                                    states[7] &&
                                    typeof states[7] === "object" &&
                                    "line1" in states[7] &&
                                    "postal_code" in states[7] &&
                                    states[8] &&
                                    typeof states[8] === "object" &&
                                    "line1" in states[8] &&
                                    typeof states[9] === "boolean";
                                const tokenHook = hooks[start + 5];
                                if (isRegistrationState && typeof tokenHook.queue?.dispatch === "function") {
                                    tokenHook.queue.dispatch(token);
                                    reactCallbackName = "registration-state-setter";
                                    return true;
                                }
                            }
                            return false;
                        };
                        const visit = (fiber) => {
                            if (!fiber || reactCallbackInvoked || visited.has(fiber)) {
                                return;
                            }
                            visited.add(fiber);
                            const props = fiber.memoizedProps;
                            if (invokeReactCallback(props)) {
                                reactCallbackInvoked = true;
                                return;
                            }
                            if (invokeRegistrationStateSetter(fiber)) {
                                reactCallbackInvoked = true;
                                return;
                            }
                            visit(fiber.child);
                            visit(fiber.sibling);
                        };
                        roots.forEach(visit);
                    }

                    return {
                        responseInputCount: responseInputs.length,
                        reactCallbackInvoked,
                        reactCallbackName,
                    };
                }
                """,
                token,
            )
            await self.page.wait_for_timeout(100)
            logger.debug(
                "Turnstile token injected "
                f"(response inputs: {injection_result['responseInputCount']}, "
                f"React callback: {injection_result['reactCallbackName'] or 'none'})"
            )
            
        except Exception as e:
            logger.warning(f"Error injecting Turnstile token: {str(e)}")
            # Don't raise error as this might not be critical
    
    async def _wait_for_verification_popup(self, timeout: int = 15) -> bool:
        """Wait for a verification popup or redirect after form submission."""
        logger.debug("Waiting for verification popup...")
        
        end_time = time.time() + timeout
        
        while time.time() < end_time:
            try:
                # Look for popup with verification content
                popup_selectors = [
                    "[role='dialog']",
                    "[role='alertdialog']",
                    ".modal",
                    ".popup",
                    ".dialog",
                    "div[style*='fixed']",
                    "div[style*='absolute']"
                ]
                
                for selector in popup_selectors:
                    popup = await self.page.query_selector(selector)
                    if popup:
                        # Check if this is a verification popup
                        text_content = await popup.text_content()
                        if any(keyword in text_content.lower() for keyword in ['verification', 'verify', 'code', '验证码', '邮件']):
                            logger.debug("Verification popup found")
                            return True
                
                # Also check for URL changes (redirects)
                current_url = self.page.url
                if 'verify' in current_url or 'confirm' in current_url:
                    logger.debug("Redirected to verification page")
                    self.current_url = current_url
                    return True
                
                await asyncio.sleep(0.5)
                
            except Exception as e:
                logger.warning(f"Error while waiting for verification popup: {str(e)}")
                await asyncio.sleep(1)
        
        logger.warning(f"No verification popup appeared within {timeout} seconds")
        return False

    async def _get_submission_error_text(self) -> str:
        """Return a visible server-side error message after a failed submit."""
        selectors = [
            "[data-sonner-toast]",
            "[role='alert']",
            "[role='status']",
            ".toast",
            ".error",
        ]
        messages = []
        for selector in selectors:
            try:
                for element in await self.page.query_selector_all(selector):
                    text = (await element.text_content() or "").strip()
                    if text:
                        messages.append(text)
            except Exception:
                continue
        return " | ".join(dict.fromkeys(messages))[:500]

    async def _get_response_error_text(self, response) -> str:
        """Extract a non-sensitive error message from a registration response."""
        try:
            payload = await response.json()
        except Exception:
            return ""

        if not isinstance(payload, dict):
            return ""
        for key in ("message", "error", "detail", "error_description"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
        return ""
    
    async def _find_verification_popup(self, timeout: int = 10) -> Optional[Any]:
        """Find and return the verification popup element"""
        try:
            # Wait for popup with various selectors
            popup_selectors = [
                f"[role='dialog']:has-text('verification')",
                f".modal:has-text('verification')",
                f"div:has-text('verification')",
                f"div:has-text('verify')",
                f"div:has-text('验证码')",
                f"div:has-text('邮件')",
                "[role='dialog']",
                ".modal",
                ".popup"
            ]
            
            for selector in popup_selectors:
                try:
                    popup = await self.page.wait_for_selector(selector, timeout=timeout * 1000)
                    if popup:
                        return popup
                except PlaywrightTimeoutError:
                    continue
            
            # Alternative method: find any popup-like element
            js_code = """
            const elements = Array.from(document.querySelectorAll('*'));
            return elements.find(el => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                const text = el.textContent || '';
                
                // Look for elements that:
                // 1. Have fixed or absolute positioning
                // 2. Are reasonably sized
                // 3. Contain verification-related text
                return (
                    (style.position === 'fixed' || style.position === 'absolute') &&
                    el.offsetWidth > 200 &&
                    el.offsetHeight > 100 &&
                    (text.includes('verification') || text.includes('verify') || 
                     text.includes('code') || text.includes('验证码') || 
                     text.includes('邮件'))
                );
            });
            """
            
            popup = await self.page.evaluate_handle(js_code)
            if popup:
                return popup.as_element()
            
            return None
            
        except Exception as e:
            logger.warning(f"Error finding verification popup: {str(e)}")
            return None
    
    async def take_screenshot(self, name: str) -> str:
        """Take a screenshot and return the file path"""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"screenshot_{name}_{timestamp}.png"
            filepath = f"/tmp/{filename}"  # Use /tmp for temporary storage
            
            await self.page.screenshot(path=filepath, full_page=True)
            self.screenshots_taken.append(filepath)
            
            logger.debug(f"Screenshot saved: {filepath}")
            return filepath
            
        except Exception as e:
            logger.warning(f"Failed to take screenshot: {str(e)}")
            return ""
    
    async def get_console_logs(self) -> List[str]:
        """Get browser console logs"""
        try:
            # Enable console logging
            console_logs = []
            
            # This would require setting up console log capturing during page initialization
            # For now, return empty list
            return console_logs
            
        except Exception as e:
            logger.warning(f"Failed to get console logs: {str(e)}")
            return []
    
    async def cleanup(self):
        """Clean up browser resources"""
        if self.camoufox:
            await self.camoufox.__aexit__(None, None, None)
        elif self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        
        self.browser = None
        self.context = None
        self.page = None
        self.playwright = None
        self.camoufox = None
        
        logger.debug("Browser resources cleaned up")
    
    async def __aenter__(self):
        """Async context manager entry"""
        await self.initialize()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.cleanup()
