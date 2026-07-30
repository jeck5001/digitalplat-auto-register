"""
Helper utilities for DigitalPlat Auto Register package

This module contains various utility functions used throughout the package.
"""

import asyncio
import re
import random
import string
import time
from typing import Callable, Any, Optional, Type, Tuple
from functools import wraps

from loguru import logger
from ..exceptions import TimeoutError, DigitalPlatError


def generate_random_username(length: int = 8, prefix: str = "user") -> str:
    """
    Generate a random username
    
    Args:
        length: Length of random part
        prefix: Username prefix
        
    Returns:
        Random username
    """
    timestamp = str(int(time.time() * 1000))[-4:]
    random_part = ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))
    return f"{prefix}_{timestamp}_{random_part}"


def generate_password(length: int = 12, include_symbols: bool = True) -> str:
    """
    Generate a secure random password
    
    Args:
        length: Password length
        include_symbols: Whether to include symbols
        
    Returns:
        Generated password
    """
    chars = string.ascii_letters + string.digits
    if include_symbols:
        chars += "!@#$%^&*"
    
    # Ensure at least one uppercase, one lowercase, and one digit
    password = (
        random.choice(string.ascii_uppercase) +
        random.choice(string.ascii_lowercase) +
        random.choice(string.digits)
    )
    
    # Fill remaining length randomly
    password += ''.join(random.choices(chars, k=length - 3))
    
    # Shuffle the password
    password_list = list(password)
    random.shuffle(password_list)
    
    return ''.join(password_list)


def generate_phone_number(country_code: str = "+1") -> str:
    """
    Generate a fake phone number
    
    Args:
        country_code: Country code to use
        
    Returns:
        Generated phone number
    """
    area_code = random.randint(100, 999)
    exchange = random.randint(100, 999)
    number = random.randint(1000, 9999)
    
    return f"{country_code}-{area_code}-{exchange}-{number}"


async def wait_for_condition(
    condition_func: Callable[[], bool],
    timeout: float = 30.0,
    check_interval: float = 1.0
) -> bool:
    """
    Wait for a condition to be true
    
    Args:
        condition_func: Function that returns True when condition is met
        timeout: Maximum time to wait in seconds
        check_interval: Time between checks in seconds
        
    Returns:
        True if condition was met, False if timed out
    """
    start_time = time.time()
    end_time = start_time + timeout
    
    while time.time() < end_time:
        try:
            if condition_func():
                logger.debug(f"Condition met after {time.time() - start_time:.2f}s")
                return True
        except Exception as e:
            logger.debug(f"Error checking condition: {str(e)}")
        
        await asyncio.sleep(check_interval)
    
    logger.warning(f"Condition not met within {timeout} seconds")
    return False


def retry_async(
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,)
):
    """
    Decorator for retrying async functions
    
    Args:
        max_attempts: Maximum number of retry attempts
        delay: Initial delay between attempts in seconds
        backoff: Multiplier for delay after each attempt
        exceptions: Exception types to catch and retry on
        
    Usage:
        @retry_async(max_attempts=3, delay=1.0)
        async def my_function():
            # Function that might fail
            pass
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            current_delay = delay
            last_exception = None
            
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    
                    if attempt < max_attempts - 1:
                        logger.warning(
                            f"Attempt {attempt + 1}/{max_attempts} failed: {str(e)}. "
                            f"Retrying in {current_delay}s..."
                        )
                        await asyncio.sleep(current_delay)
                        current_delay *= backoff
                    else:
                        logger.error(
                            f"All {max_attempts} attempts failed. Last error: {str(e)}"
                        )
            
            # Re-raise the last exception
            raise last_exception
        
        return wrapper
    return decorator


def parse_email_content(html_content: str) -> str:
    """
    Parse HTML email content to extract plain text
    
    Args:
        html_content: HTML email content
        
    Returns:
        Plain text content
    """
    if not html_content:
        return ""
    
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Remove script and style elements
        for script in soup(["script", "style"]):
            script.decompose()
        
        # Get text and clean it up
        text = soup.get_text()
        
        # Break into lines and remove leading/trailing space
        lines = (line.strip() for line in text.splitlines())
        # Break multi-headlines into a line each
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        # Drop blank lines
        text = '\n'.join(chunk for chunk in chunks if chunk)
        
        return text
        
    except Exception as e:
        logger.warning(f"Error parsing email content: {str(e)}")
        return html_content  # Return original if parsing fails


def extract_verification_code(content: str) -> Optional[str]:
    """
    Extract verification code from text content
    
    Args:
        content: Text content to search
        
    Returns:
        Verification code if found, None otherwise
    """
    if not content:
        return None
    
    try:
        # Look for verification code patterns
        patterns = [
            r'(?:code|验证码|verify|确认).*?(\d{6})',
            r'(?:code|验证码|verify|确认).*?(\d{4})',
            r'your code is[:\s]*(\d{6})',
            r'code[:\s]*(\d{6})',
            r'验证码[:\s]*(\d{6})',
            r'\b(\d{6})\b',  # Any 6-digit number
            r'\b(\d{4})\b'   # Any 4-digit number
        ]
        
        for pattern in patterns:
            match = re.search(pattern, content, re.IGNORECASE | re.MULTILINE)
            if match:
                code = match.group(1)
                logger.debug(f"Found verification code using pattern: {pattern}")
                return code
        
        return None
        
    except Exception as e:
        logger.warning(f"Error extracting verification code: {str(e)}")
        return None


def validate_email_address(email: str) -> bool:
    """
    Validate email address format
    
    Args:
        email: Email address to validate
        
    Returns:
        True if email is valid format
    """
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email))


def safe_getattr(obj: Any, attr_path: str, default: Any = None) -> Any:
    """
    Safely get nested attribute from object
    
    Args:
        obj: Object to get attribute from
        attr_path: Dot-separated attribute path (e.g., "a.b.c")
        default: Default value if attribute not found
        
    Returns:
        Attribute value or default
    """
    try:
        attrs = attr_path.split('.')
        current = obj
        
        for attr in attrs:
            if hasattr(current, attr):
                current = getattr(current, attr)
            else:
                return default
        
        return current
        
    except Exception:
        return default


def measure_time(func):
    """
    Decorator to measure function execution time
    
    Usage:
        @measure_time
        def my_function():
            # Function to measure
            pass
    """
    @wraps(func)
    async def async_wrapper(*args, **kwargs):
        start_time = time.time()
        result = await func(*args, **kwargs)
        duration = time.time() - start_time
        logger.debug(f"Async function {func.__name__} took {duration:.2f}s")
        return result
    
    @wraps(func)
    def sync_wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        duration = time.time() - start_time
        logger.debug(f"Function {func.__name__} took {duration:.2f}s")
        return result
    
    # Choose wrapper based on whether function is async
    if asyncio.iscoroutinefunction(func):
        return async_wrapper
    else:
        return sync_wrapper


def chunk_list(lst: list, chunk_size: int) -> list:
    """
    Split a list into chunks of specified size
    
    Args:
        lst: List to split
        chunk_size: Size of each chunk
        
    Returns:
        List of chunks
    """
    return [lst[i:i + chunk_size] for i in range(0, len(lst), chunk_size)]