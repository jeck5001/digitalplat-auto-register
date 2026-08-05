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
    
    async def _get_domains(self) -> List[str]:
        """Fetch available email domains from mail.td"""
        client = await self._get_client()
        resp = await client.get("/api/domains")
        
        if resp.status_code != 200:
            raise EmailServiceError(f"Failed to get domains: {resp.status_code} {resp.text[:200]}")
        
        data = resp.json()
        domains = data.get("domains", [])
        active_domains = [d["domain"] for d in domains]
        
        if not active_domains:
            active_domains = [d["domain"] for d in domains]
        
        if not active_domains:
            raise EmailServiceError("No available domains from mail.td")
        
        self.domains = active_domains
        return active_domains
    
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
            domains = await self._get_domains()
            domain = random.choice(domains)
            
            # Generate random email and password
            email_user = self._generate_email_user(6)
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
        check_interval: int = 5
    ) -> VerificationEmailResult:
        """
        Check for DigitalPlat verification email using mail.td HTTP API.
        
        Args:
            email: Email address to check
            timeout: Maximum time to wait for email in seconds
            check_interval: Time between checks in seconds
            
        Returns:
            VerificationEmailResult with verification code if found
        """
        logger.info(f"Waiting for verification email at {email}")
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
                logger.debug(f"Checking for verification email (attempt {check_count})")
                
                try:
                    messages = await self._fetch_messages()
                    
                    if messages:
                        logger.debug(f"Received {len(messages)} email(s)")
                        # Debug: log ALL received emails
                        for msg in messages:
                            logger.info(
                                f"  Email: subject='{msg.get('subject', '(empty)')}' "
                                f"from='{msg.get('sender', {}).get('address', '(empty)')}'"
                            )
                        
                        # Look for verification email
                        for msg in messages:
                            if self._is_verification_email(msg):
                                full_msg = await self._fetch_message_detail(msg["id"])
                                if not full_msg:
                                    full_msg = msg
                                
                                logger.info(f"  verification email found: {full_msg.get('subject')}")
                                
                                # Debug: log the full content
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
                                            "sender": full_msg.get("sender", {}).get("address", ""),
                                            "message_id": full_msg.get("id", ""),
                                        }
                                    )
                                    logger.info(f"Found verification code: {code} ({duration:.2f}s)")
                                    return result
                                else:
                                    logger.warning(
                                        f"Email matched but no code extracted from: {full_msg.get('subject', '')}"
                                    )
                                    # Save full content for debugging
                                    logger.info(f"Full message JSON: {json.dumps(full_msg, indent=2, ensure_ascii=False)[:2000]}")
                    else:
                        logger.debug(f"  No emails yet (check {check_count})")
                
                except Exception as e:
                    logger.debug(f"Error checking emails: {str(e)}")
                
                # Wait before next check
                if time.time() + check_interval < end_time:
                    await asyncio.sleep(check_interval)
                else:
                    break
            
            # Timeout - log what we did receive for debugging
            duration = time.time() - start_time
            logger.warning(f"No verification email found in {timeout}s")
            
            # Final attempt: log all messages for debugging
            try:
                final_messages = await self._fetch_messages()
                if final_messages:
                    logger.warning(f"Found {len(final_messages)} email(s) but none matched verification patterns:")
                    for msg in final_messages:
                        logger.warning(f"  - '{msg.get('subject')}' from {msg.get('sender', {}).get('address')}")
            except Exception:
                pass
            
            return VerificationEmailResult(
                found=False,
                duration=duration,
                error=f"Verification email not received within {timeout} seconds",
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
                            sender_info = msg.get("sender", {})
                            sender_address = sender_info.get("address", "")
                            sender_name = sender_info.get("name", "")
                            sender_text = f"{sender_name} {sender_address}".strip()
                            
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
    
    def _is_verification_email(self, message: Dict[str, Any]) -> bool:
        """Check if a message looks like a verification email"""
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
            f"{message.get('sender', {}).get('name', '')} "
            f"{message.get('sender', {}).get('address', '')}"
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
