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


_INJECT_MAIN_WORLD_SOURCE = r"""
async function (token) {
    // Runs inside the page's own world, where React's expando keys and
    // window.turnstile actually exist.

    const result = {
        responseInputCount: 0,
        reactCallbackName: null,
        stateIndex: null,
        buttonDisabled: null,
    };

    const fiberKeyOf = (element) =>
        Object.keys(element).find((key) => key.indexOf("__reactFiber$") === 0);

    const findButton = () =>
        [...document.querySelectorAll("button")].find(
            (button) => (button.innerText || "").trim() === "Register"
        );

    // 1. Fill the hidden response input the way a real widget would.
    const responseInputs = [
        ...document.querySelectorAll(
            "input[data-cf-turnstile], input[name='cf-turnstile-response']"
        ),
    ];
    result.responseInputCount = responseInputs.length;
    for (const input of responseInputs) {
        const setter = Object.getOwnPropertyDescriptor(
            HTMLInputElement.prototype,
            "value"
        ).set;
        if (setter) {
            setter.call(input, token);
        } else {
            input.value = token;
        }
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new Event("change", { bubbles: true }));
    }
    window.cf_turnstile_response = token;

    // 2. Locate the component that owns the form: the nearest fiber with a
    //    substantial hook chain.
    const form = document.querySelector("form");
    const formKey = form ? fiberKeyOf(form) : null;
    let owner = null;
    if (formKey) {
        let fiber = form[formKey];
        for (let depth = 0; fiber && depth < 24; depth += 1) {
            let hookCount = 0;
            for (let hook = fiber.memoizedState; hook && hookCount < 80; hook = hook.next) {
                hookCount += 1;
            }
            if (hookCount >= 20) {
                owner = fiber;
                break;
            }
            fiber = fiber.return;
        }
    }

    // 3. Prefer a real token callback prop if the page exposes one.
    const callbackNames = [
        "onTokenChange", "onSuccess", "onVerify", "onToken", "onChange",
    ];
    if (owner) {
        let fiber = owner;
        for (let depth = 0; fiber && depth < 40; depth += 1) {
            const props = fiber.memoizedProps;
            if (props && typeof props === "object") {
                const name = callbackNames.find(
                    (candidate) => typeof props[candidate] === "function"
                );
                if (name) {
                    try {
                        props[name](token);
                        result.reactCallbackName = name;
                    } catch (error) {
                        result.reactCallbackName = null;
                    }
                }
            }
            if (result.reactCallbackName) {
                break;
            }
            fiber = fiber.return;
        }
    }

    // 4. Otherwise dispatch into the state slot that gates the submit button.
    //    The page keeps the token in a useState whose value is "" until the
    //    widget succeeds, but several other states are empty strings too, and
    //    which index holds the token changes when the page is rebuilt. So try
    //    every empty string slot and keep the one that actually enables the
    //    button, restoring the others as we go.
    if (!result.reactCallbackName && owner) {
        const candidates = [];
        let index = 0;
        for (let hook = owner.memoizedState; hook; hook = hook.next, index += 1) {
            const state = hook.memoizedState;
            if (typeof hook.queue?.dispatch !== "function") {
                continue;
            }
            if (typeof state !== "string" || state.length !== 0) {
                continue;
            }
            candidates.push({ index: index, hook: hook });
        }
        result.candidates = candidates.map((c) => c.index);

        for (const candidate of candidates) {
            try {
                candidate.hook.queue.dispatch(token);
            } catch (error) {
                continue;
            }
            // React commits asynchronously; read the button on the next tick.
            const enabled = await new Promise((resolve) => {
                setTimeout(() => {
                    const button = findButton();
                    resolve(button ? !button.disabled : false);
                }, 60);
            });
            if (enabled) {
                result.stateIndex = candidate.index;
                result.reactCallbackName = "state-dispatch";
                break;
            }
            // Not the token slot: put it back the way it was.
            try {
                candidate.hook.queue.dispatch("");
            } catch (error) {
                // Leaving it set is harmless if it is not the gate.
            }
        }
    }

    const button = findButton();
    result.buttonDisabled = button ? button.disabled : null;
    document.documentElement.setAttribute("data-dsh-inject", JSON.stringify(result));
}
"""


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
            
            # Define form fields. Selectors use the form's stable `name`
            # attributes, not visible labels or placeholders: the current
            # registration page is localised, its address inputs carry no
            # placeholder at all, and the address fields are named
            # contact_* (e.g. contact_line1, contact_region,
            # contact_country), not the old English placeholders.
            form_fields = [
                FormField("username", "input[name='username']", "text", placeholder="Username"),
                FormField("email", "input[name='email']", "email", placeholder="Email Address"),
                FormField("fullname", "input[name='fullname']", "text", placeholder="Full Name"),
                FormField("phone_country", "select[name='phone_country']", "select"),
                FormField("phone", "input[name='phone']", "tel", placeholder="Phone Number"),
                FormField("password", "input[name='password']", "password", placeholder="Password"),
                FormField("referral_code", "input[name='referral_code']", "text", required=False),
                FormField("address_line1", "input[name='contact_line1']", "text"),
                FormField("address_line2", "input[name='contact_line2']", "text", required=False),
                FormField("city", "input[name='contact_city']", "text"),
                FormField("state", "input[name='contact_region']", "text"),
                FormField("postal_code", "input[name='contact_postal_code']", "text"),
                FormField("country", "select[name='contact_country']", "select"),
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
            
            # Look for submit button. The form has a single button, so
            # prefer it by position over its localised text: the current
            # page renders "Register" in English and "注册" in Chinese,
            # and a text-only lookup breaks on the other locale.
            submit_selectors = [
                "form button[type='submit']",
                "form button",
                "button[type='submit']",
                "button:has-text('注册')",
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
            
            # The button is disabled until the Turnstile token lands in
            # React state, which can lag the injection by a moment. Poll
            # briefly before declaring the submit dead, and if it never
            # enables, report which fields are actually invalid instead
            # of the old generic message.
            for _ in range(10):
                if await submit_button.is_enabled():
                    break
                await asyncio.sleep(0.5)

            if not await submit_button.is_enabled():
                diagnostics = await self._get_form_diagnostics()
                detail = f" ({diagnostics})" if diagnostics else ""
                raise BrowserAutomationError(
                    "Registration submit button is disabled after form filling; "
                    "verify the contact fields and Turnstile token" + detail
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
            # The phone country dropdown is a separate control from the
            # phone input; it takes the same country as the address.
            "phone_country": user_profile.country,
        }
        
        value = field_value_map.get(field.name, "")

        if field.name == "phone" and value:
            # The phone input is a *national* number field: the country
            # calling code is sent separately via phone_country. Filling
            # a full "+1-..." value here is what the server rejects as
            # an invalid phone number, so strip it down to digits and
            # drop a leading US country code (1) if present.
            import re as _re

            digits = _re.sub(r"\D", "", value)
            if len(digits) == 11 and digits.startswith("1"):
                digits = digits[1:]
            value = digits
        
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
        """Check the form option that reuses WHOIS data for billing.

        The checkbox is named billing_same and is checked by default on
        the current page; match it by name, not by its localised label
        text (the old English label no longer exists). If the control
        is gone, do not fail the whole registration: the form then
        simply has no separate billing address to reuse.
        """
        checkbox = self.page.locator("input[name='billing_same']")
        if await checkbox.count() == 0:
            logger.warning("Billing address checkbox not found; continuing without it")
            return

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
        """Inject the Turnstile token into the page's own React state.

        Two things make this harder than writing the hidden input:

        1. ``page.evaluate`` runs in an isolated world. ``window.turnstile``,
           ``window.__next_f`` and React's ``__reactFiber$`` expando keys are
           invisible there, so every fiber walk started from evaluate finds
           nothing. The work has to happen inside the page's own world.
        2. The form keeps the token in component state and leaves its submit
           button disabled until that state holds a token, so filling only
           ``input[name='cf-turnstile-response']`` is not enough.

        The routine below runs as a <script> element (the page's world), fills
        the hidden input, then dispatches the token into the owning component's
        empty-string state slot and reports the outcome through a DOM
        attribute, which both worlds can read.
        """
        try:
            await self.page.locator("input[name='cf-turnstile-response']").wait_for(
                state="attached",
                timeout=min(self.browser_config.timeout, 15000),
            )

            injection_result = await self.page.evaluate(
                """
                async ({ token, source }) => {
                    document.documentElement.removeAttribute("data-dsh-inject");

                    // A <script> element is the only path into the page's own
                    // world; evaluate itself runs isolated from it.
                    const script = document.createElement("script");
                    script.textContent =
                        "(" + source + ")(" + JSON.stringify(token) + ");";
                    document.head.appendChild(script);
                    script.remove();

                    // The page-world routine is async, so wait for it to
                    // publish its report instead of reading immediately.
                    const deadline = Date.now() + 5000;
                    while (Date.now() < deadline) {
                        const raw = document.documentElement.getAttribute(
                            "data-dsh-inject"
                        );
                        if (raw) {
                            return JSON.parse(raw);
                        }
                        await new Promise((resolve) => setTimeout(resolve, 50));
                    }
                    return { error: "no report" };
                }
                """,
                {"token": token, "source": _INJECT_MAIN_WORLD_SOURCE},
            )

            await self.page.wait_for_timeout(150)
            logger.debug(f"Turnstile injection result: {injection_result}")

            if injection_result.get("buttonDisabled") is not False:
                logger.warning(
                    "Turnstile token was written but the submit button is still "
                    f"disabled ({injection_result}); the submit step reports "
                    "which form fields are invalid"
                )
        except Exception as e:
            logger.warning(f"Error injecting Turnstile token: {str(e)}")
            # Not fatal on its own: the submit step reports the real blocker.

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

    async def _get_form_diagnostics(self) -> str:
        """List form fields that fail native validation, by name only.

        Used when the submit button never enables: it tells the next
        run (and the job log) whether the blocker is an empty/invalid
        field or the Turnstile token, without logging field values.
        """
        try:
            return await self.page.evaluate(
                """
                () => {
                    const form = document.querySelector("form");
                    if (!form) return "";
                    const problems = [];
                    for (const el of form.querySelectorAll("input, select")) {
                        if (el.type === "hidden") continue;
                        if (el.name === "cf-turnstile-response") continue;
                        if (!el.checkValidity()) {
                            problems.push(el.name || el.id || el.type);
                        }
                    }
                    const turnstile = document.querySelector(
                        "input[name='cf-turnstile-response']"
                    );
                    if (!turnstile || !turnstile.value) {
                        problems.push("turnstile-token-missing");
                    }
                    return problems.join(", ");
                }
                """
            )
        except Exception:
            return ""

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
