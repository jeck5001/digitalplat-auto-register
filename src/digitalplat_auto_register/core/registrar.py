"""
DigitalPlat Registration Core Service

This module provides the main orchestration logic that coordinates all components
to complete a DigitalPlat account registration process end-to-end.

The registration workflow:
1. Turnstile token acquisition
2. Temporary email creation  
3. Browser navigation to registration
4. Form filling and submission
5. Email verification code retrieval
6. Verification popup handling
7. Registration completion
"""

import asyncio
import time
import random
import string
from datetime import datetime
from typing import Optional, Dict, Any, Callable

from loguru import logger

from ..types import (
    DigitalPlatConfig, UserProfile, RegistrationStatus,
    EmailProvider, TurnstileSolverType
)
from ..core.result import (
    RegistrationResult, StepResult, TurnstileResult, 
    EmailResult, VerificationEmailResult, BrowserResult
)
from ..services.turnstile_solver import TurnstileSolver
from ..services.email_service import EmailService, EmailServiceFactory
from ..services.browser_automation import BrowserAutomationService
from ..exceptions import RegistrationError, EmailServiceError


_DEFAULT_FIRST_NAMES = ("Alex", "Casey", "Jordan", "Morgan", "Riley", "Taylor")
_DEFAULT_LAST_NAMES = ("Brown", "Davis", "Garcia", "Johnson", "Miller", "Wilson")
_DEFAULT_US_LOCATIONS = (
    ("1200 East Monroe Street", "Phoenix", "AZ", "85004"),
    ("500 West Madison Street", "Chicago", "IL", "60661"),
    ("200 Biscayne Boulevard", "Miami", "FL", "33131"),
    ("901 Market Street", "San Francisco", "CA", "94103"),
    ("700 5th Avenue", "Seattle", "WA", "98104"),
)


class DigitalPlatRegistrar:
    """
    Main orchestration class for DigitalPlat account registration
    
    This class manages the complete registration workflow by coordinating:
    - Turnstile token acquisition
    - Temporary email creation and management
    - Browser automation for form interaction
    - Email verification code retrieval
    - Verification popup handling
    - Progress tracking and error handling
    """
    
    def __init__(self, config: DigitalPlatConfig):
        """
        Initialize the registrar with configuration
        
        Args:
            config: DigitalPlat configuration
        """
        self.config = config
        
        # Service instances
        self.turnstile_solver: Optional[TurnstileSolver] = None
        self.email_service: Optional[EmailService] = None
        self.browser_service: Optional[BrowserAutomationService] = None
        
        # Registration state
        self.registration_result: Optional[RegistrationResult] = None
        
        logger.debug("DigitalPlatRegistrar initialized")
    
    async def register_account(
        self,
        username: Optional[str] = None,
        email: Optional[str] = None,
        fullname: Optional[str] = None,
        phone: Optional[str] = None,
        password: Optional[str] = None,
        address_line1: Optional[str] = None,
        address_line2: Optional[str] = None,
        city: Optional[str] = None,
        state: Optional[str] = None,
        postal_code: Optional[str] = None,
        country: Optional[str] = None,
        referral_code: str = "",
        on_step_complete: Optional[Callable[[StepResult], None]] = None
    ) -> RegistrationResult:
        """
        Perform complete DigitalPlat account registration
        
        Args:
            username: Account username (auto-generated if None)
            email: Email address for account (auto-created if None)
            fullname: Full name for account
            phone: Phone number for account
            password: Account password (auto-generated if None)
            address_line1: Address line 1
            address_line2: Address line 2
            city: City
            state: State/Province
            postal_code: Postal/ZIP code
            country: Country code
            referral_code: Referral code to use
            on_step_complete: Optional callback for step completion notifications
            
        Returns:
            RegistrationResult with complete registration status and details
        """
        
        # Initialize registration result
        self.registration_result = RegistrationResult(
            success=False,
            registration_status=RegistrationStatus.PENDING,
            referral_code=referral_code
        )
        
        try:
            logger.info("Starting DigitalPlat account registration...")
            
            # Generate or validate user profile data
            user_profile = self._prepare_user_profile(
                username, email, fullname, phone, password,
                address_line1, address_line2, city, state, postal_code, country
            )
            
            self.registration_result.username = user_profile.username
            
            # Execute registration workflow with retries
            for attempt in range(self.config.max_registration_attempts):
                try:
                    if attempt > 0:
                        logger.info(f"Registration attempt {attempt + 1}/{self.config.max_registration_attempts}")
                        await asyncio.sleep(self.config.retry_delay)
                    
                    result = await self._execute_registration_workflow(
                        user_profile, referral_code, on_step_complete
                    )
                    
                    if result.success:
                        return result
                    
                except Exception as e:
                    logger.error(f"Registration attempt {attempt + 1} failed: {str(e)}")
                    
                    if attempt == self.config.max_registration_attempts - 1:
                        # Last attempt failed
                        return self.registration_result.mark_failed(
                            str(e), "registration_workflow"
                        )
                    
                    continue
            
            return self.registration_result
            
        except Exception as e:
            logger.error(f"Registration failed with exception: {str(e)}")
            return self.registration_result.mark_failed(str(e), "initialization")
    
    async def _execute_registration_workflow(
        self,
        user_profile: UserProfile,
        referral_code: str,
        on_step_complete: Optional[Callable[[StepResult], None]]
    ) -> RegistrationResult:
        """
        Execute the complete registration workflow
        
        Args:
            user_profile: User profile information
            referral_code: Referral code to use
            on_step_complete: Optional step completion callback
            
        Returns:
            RegistrationResult with workflow results
        """
        
        try:
            # Step 1: Initialize services
            await self._initialize_services()
            
            # Step 2: Get Turnstile token
            turnstile_result = await self._acquire_turnstile_token()
            self.registration_result.turnstile_result = turnstile_result
            
            if not turnstile_result.success:
                raise RegistrationError("Failed to acquire Turnstile token", "turnstile")
            
            self._add_step_result("turnstile_token_acquisition", StepResult(
                name="turnstile_token_acquisition",
                status="completed",
                success=True,
                duration=turnstile_result.duration or 0,
                message="Turnstile token acquired successfully"
            ), on_step_complete)
            
            # Step 3: Create temporary email
            if not user_profile.email:
                email_result = await self._create_temporary_email()
                self.registration_result.email_result = email_result
                user_profile.email = email_result.email
            else:
                # Use provided email
                email_result = EmailResult(
                    success=True,
                    email=user_profile.email,
                    provider=self.config.email.provider.value,
                    created_at=datetime.now()
                )
                self.registration_result.email_result = email_result
            
            self.registration_result.email = user_profile.email
            
            if not email_result.success:
                raise RegistrationError("Failed to create temporary email", "email_creation")
            
            self._add_step_result("email_creation", StepResult(
                name="email_creation",
                status="completed",
                success=True,
                duration=email_result.duration or 0,
                message=f"Email created: {email_result.email}"
            ), on_step_complete)
            
            # Step 4: Navigate to registration page
            browser_nav_result = await self._navigate_to_registration(referral_code)
            self.registration_result.browser_result = browser_nav_result
            
            if not browser_nav_result.success:
                raise RegistrationError("Failed to navigate to registration page", "browser_navigation")
            
            self._add_step_result("browser_navigation", StepResult(
                name="browser_navigation",
                status="completed",
                success=True,
                duration=browser_nav_result.duration or 0,
                message=f"Navigated to registration page: {browser_nav_result.url}"
            ), on_step_complete)
            
            # Step 5: Fill and submit registration form
            browser_form_result = await self._fill_and_submit_form(user_profile, turnstile_result.token)
            
            if not browser_form_result.success:
                raise RegistrationError("Failed to submit registration form", "form_submission")
            
            self._add_step_result("form_submission", StepResult(
                name="form_submission",
                status="completed",
                success=True,
                duration=browser_form_result.duration or 0,
                message="Registration form submitted successfully"
            ), on_step_complete)
            
            # Step 6: Wait for and retrieve verification email
            verification_result = await self._wait_for_verification_email(user_profile.email)
            self.registration_result.verification_result = verification_result
            
            if not verification_result.found:
                raise RegistrationError("Failed to receive verification email", "email_verification")
            
            self._add_step_result("verification_email_retrieval", StepResult(
                name="verification_email_retrieval",
                status="completed",
                success=True,
                duration=verification_result.duration or 0,
                message="Verification code retrieved"
            ), on_step_complete)
            
            # Step 7: Handle verification popup
            browser_verify_result = await self._handle_verification_popup(verification_result.code)
            
            if not browser_verify_result.success:
                raise RegistrationError("Failed to complete verification", "verification_popup")
            
            self._add_step_result("verification_completion", StepResult(
                name="verification_completion", 
                status="completed",
                success=True,
                duration=browser_verify_result.duration or 0,
                message="Email verification completed successfully"
            ), on_step_complete)
            
            # Registration completed successfully!
            self.registration_result.mark_success()
            self.registration_result.email_verified = True
            self.registration_result.account_created = True
            self.registration_result.final_url = browser_verify_result.url
            
            if browser_verify_result.screenshot:
                self.registration_result.browser_result.screenshot = browser_verify_result.screenshot
            
            logger.info(f"Registration completed successfully: {self.registration_result.username}")
            return self.registration_result
            
        except Exception as e:
            duration = time.time() - self.registration_result.start_time.timestamp()
            logger.error(f"Registration workflow failed: {str(e)}")
            
            return self.registration_result.mark_failed(str(e), "workflow_execution")
        
        finally:
            # Always clean up services
            await self._cleanup_services()
    
    async def _initialize_services(self):
        """Initialize all required services"""
        logger.debug("Initializing services...")
        
        # Initialize Turnstile solver
        self.turnstile_solver = TurnstileSolver(self.config.turnstile)
        
        # Initialize email service
        email_config = self.config.email.copy()
        self.email_service = EmailServiceFactory.create_service(email_config)
        
        # Initialize browser service 
        self.browser_service = BrowserAutomationService(
            self.config.browser, 
            self.config.proxy
        )
        
        await self.browser_service.initialize()
    
    async def _cleanup_services(self):
        """Clean up all services"""
        logger.debug("Cleaning up services...")
        
        if self.turnstile_solver:
            self.turnstile_solver.close()
        
        if self.email_service:
            if hasattr(self.email_service, 'close'):
                await self.email_service.close()
        
        if self.browser_service:
            await self.browser_service.cleanup()
    
    async def _acquire_turnstile_token(self) -> TurnstileResult:
        """Acquire Turnstile token from solver"""
        logger.info("Acquiring Turnstile token...")
        
        if not self.turnstile_solver:
            raise RegistrationError("Turnstile solver not initialized", "service_initialization")
        
        website_url = f"{self.config.base_url}{self.config.registration_endpoint}"
        website_key = self.config.turnstile.sitekey
        
        return await self.turnstile_solver.get_token(
            website_url=website_url,
            website_key=website_key
        )
    
    async def _create_temporary_email(self) -> EmailResult:
        """Create temporary email address"""
        logger.info("Creating temporary email address...")
        
        if not self.email_service:
            raise RegistrationError("Email service not initialized", "service_initialization")
        
        return await self.email_service.create_temporary_email()
    
    async def _navigate_to_registration(self, referral_code: str) -> BrowserResult:
        """Navigate to registration page"""
        logger.info("Navigating to registration page...")
        
        if not self.browser_service:
            raise RegistrationError("Browser service not initialized", "service_initialization")
        
        return await self.browser_service.navigate_to_registration(
            str(self.config.base_url), 
            referral_code
        )
    
    async def _fill_and_submit_form(self, user_profile: UserProfile, turnstile_token: str) -> BrowserResult:
        """Fill registration form and submit"""
        logger.info("Filling and submitting registration form...")
        
        if not self.browser_service:
            raise RegistrationError("Browser service not initialized", "service_initialization")
        
        # Fill form
        fill_result = await self.browser_service.fill_registration_form(
            user_profile, turnstile_token
        )
        
        if not fill_result.success:
            return fill_result
        
        # Submit form
        submit_result = await self.browser_service.submit_registration_form()
        return submit_result
    
    async def _wait_for_verification_email(self, email: str) -> VerificationEmailResult:
        """Wait for and retrieve verification email"""
        logger.info(f"Waiting for verification email: {email}")
        
        if not self.email_service:
            raise RegistrationError("Email service not initialized", "service_initialization")
        
        return await self.email_service.check_verification_email(
            email,
            timeout=self.config.verification_timeout,
            check_interval=self.config.verification_check_interval
        )
    
    async def _handle_verification_popup(self, verification_code: str) -> BrowserResult:
        """Handle verification popup with the received code"""
        logger.info("Handling verification popup...")
        
        if not self.browser_service:
            raise RegistrationError("Browser service not initialized", "service_initialization")
        
        return await self.browser_service.handle_verification_popup(
            verification_code
        )
    
    def _prepare_user_profile(
        self,
        username: Optional[str],
        email: Optional[str], 
        fullname: Optional[str],
        phone: Optional[str],
        password: Optional[str],
        address_line1: Optional[str],
        address_line2: Optional[str],
        city: Optional[str],
        state: Optional[str],
        postal_code: Optional[str],
        country: Optional[str]
    ) -> UserProfile:
        """Prepare and validate user profile data"""
        logger.debug("Preparing user profile...")
        
        # Generate username if not provided
        if not username:
            timestamp = str(int(time.time()))[-6:]
            random_part = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
            username = f"{self.config.default_username_prefix}_{timestamp}_{random_part}"
        
        # Generate password if not provided
        if not password:
            chars = string.ascii_letters + string.digits + "!@#$%^&*"
            password = ''.join(random.choices(chars, k=self.config.default_password_length))
        
        default_address, default_city, default_state, default_postal_code = random.choice(
            _DEFAULT_US_LOCATIONS
        )

        # Compose callers can omit registration data; fill required form fields.
        if not fullname:
            fullname = f"{random.choice(_DEFAULT_FIRST_NAMES)} {random.choice(_DEFAULT_LAST_NAMES)}"
        
        if not phone:
            phone = f"+1-555-{random.randint(100, 999)}-{random.randint(1000, 9999)}"
        
        if not country:
            country = "US"
        
        return UserProfile(
            username=username,
            email=email,  # Can be None, will be created
            fullname=fullname,
            phone=phone,
            password=password,
            address_line1=address_line1 or default_address,
            address_line2=address_line2 or "",
            city=city or default_city,
            state=state or default_state,
            postal_code=postal_code or default_postal_code,
            country=country
        )
    
    def _add_step_result(
        self, 
        step_name: str, 
        step_result: StepResult,
        on_step_complete: Optional[Callable[[StepResult], None]]
    ):
        """Add step result and optionally call completion callback"""
        self.registration_result.add_step_result(step_result)
        
        if on_step_complete:
            try:
                on_step_complete(step_result)
            except Exception as e:
                logger.warning(f"Step completion callback failed: {str(e)}")


# Convenience functions for common use cases

async def register_with_defaults(
    referral_code: str = "",
    config_file: Optional[str] = None,
    **kwargs
) -> RegistrationResult:
    """
    Register a DigitalPlat account with default settings
    
    Args:
        referral_code: Referral code to use
        config_file: Optional configuration file path
        **kwargs: Additional arguments passed to register_account
        
    Returns:
        RegistrationResult with registration status
    """
    
    # Environment values provide the container defaults; an optional file can
    # override individual settings for local or NAS-specific deployments.
    from .config import ConfigManager

    config_manager = ConfigManager().load_from_env()
    if config_file:
        config_manager.load_from_file(config_file)
    config = config_manager.load()
    
    # Create registrar and register
    registrar = DigitalPlatRegistrar(config)
    result = await registrar.register_account(
        referral_code=referral_code,
        **kwargs
    )
    
    return result
