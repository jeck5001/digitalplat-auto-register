"""
Cloudflare Turnstile Solver Service

This module provides functionality to obtain Cloudflare Turnstile tokens
from various solver services to bypass CAPTCHA verification during
DigitalPlat registration.
"""

import asyncio
import json
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Union
import requests
from loguru import logger

from ..types import TurnstileConfig, TurnstileSolverType
from ..core.result import TurnstileResult
from ..exceptions import TurnstileSolverError, NetworkError


class TurnstileSolver:
    """
    Cloudflare Turnstile token acquisition service.
    
    Supports multiple solver backends:
    - Remote HTTP API services (e.g., 2captcha, anti-captcha)
    - Local solver processes
    - Mock service for testing
    """
    
    def __init__(self, config: TurnstileConfig):
        """
        Initialize the Turnstile solver
        
        Args:
            config: Turnstile solver configuration
        """
        self.config = config
        self.session = requests.Session()

        logger.debug(f"Initialized Turnstile solver: {config.solver_type}")
    
    async def get_token(
        self,
        website_url: str,
        website_key: str,
        action: Optional[str] = None,
        data: Optional[str] = None,
        pagedata: Optional[str] = None,
        user_agent: Optional[str] = None
    ) -> TurnstileResult:
        """
        Get a Turnstile token for the specified website
        
        Args:
            website_url: URL of the website with Turnstile
            website_key: Turnstile sitekey
            action: Turnstile action parameter (optional)
            data: Turnstile data parameter (optional) 
            pagedata: Turnstile pagedata parameter (optional)
            user_agent: User-Agent string to use (optional)
            
        Returns:
            TurnstileResult containing the token and metadata
            
        Raises:
            TurnstileSolverError: If token acquisition fails
        """
        try:
            logger.info(f"Requesting Turnstile token for {website_url}")
            start_time = time.time()
            
            if self.config.solver_type == TurnstileSolverType.REMOTE:
                token = await self._get_remote_token(
                    website_url, website_key, action, data, pagedata, user_agent
                )
            elif self.config.solver_type == TurnstileSolverType.LOCAL:
                token = await self._get_local_token(
                    website_url, website_key, action, data, pagedata, user_agent
                )
            elif self.config.solver_type == TurnstileSolverType.MOCK:
                token = await self._get_mock_token()
            else:
                raise TurnstileSolverError(f"Unknown solver type: {self.config.solver_type}")
            
            duration = time.time() - start_time
            
            # Assume token expires in 5 minutes (typical for Turnstile)
            expires_at = datetime.now() + timedelta(minutes=5)
            
            result = TurnstileResult(
                success=True,
                token=token,
                solver_type=self.config.solver_type.value,
                created_at=datetime.now(),
                expires_at=expires_at,
                duration=duration
            )
            
            logger.info(f"Successfully obtained Turnstile token in {duration:.2f}s")
            return result
            
        except Exception as e:
            duration = time.time() - start_time if 'start_time' in locals() else 0
            error_msg = f"Failed to obtain Turnstile token: {str(e)}"
            logger.error(error_msg)
            
            return TurnstileResult(
                success=False,
                error=error_msg,
                solver_type=self.config.solver_type.value,
                duration=duration
            )
    
    async def _get_remote_token(
        self,
        website_url: str,
        website_key: str,
        action: Optional[str] = None,
        data: Optional[str] = None,
        pagedata: Optional[str] = None,
        user_agent: Optional[str] = None
    ) -> str:
        """
        Get token from remote HTTP API service
        
        Args:
            website_url: URL of the website 
            website_key: Turnstile sitekey
            action: Turnstile action parameter
            data: Turnstile data parameter
            pagedata: Turnstile pagedata parameter
            user_agent: User-Agent string
            
        Returns:
            Turnstile token string
            
        Raises:
            TurnstileSolverError: If the remote solver fails
            NetworkError: If network communication fails
        """
        try:
            # Prepare task creation payload
            task_payload = {
                "type": "TurnstileTaskProxyless",
                "websiteURL": website_url,
                "websiteKey": website_key,
            }
            
            # Add optional parameters if provided
            if action:
                task_payload["action"] = action
            if data:
                task_payload["data"] = data
            if pagedata:
                task_payload["pageData"] = pagedata
            if user_agent:
                task_payload["userAgent"] = user_agent
            
            # Create task
            create_url = f"{self.config.remote_endpoint.rstrip('/')}/createTask"
            logger.debug(f"Creating task at {create_url}")
            
            response = self.session.post(
                create_url,
                json={"task": task_payload},
                headers={"Content-Type": "application/json"},
                timeout=self.config.timeout,
            )
            
            if response.status_code != 200:
                raise NetworkError(
                    f"Failed to create task: {response.status_code} {response.text}",
                    url=create_url,
                    status_code=response.status_code
                )
            
            task_data = response.json()
            
            if task_data.get("errorId", 0) != 0:
                error_msg = task_data.get("errorDescription", "Unknown error")
                raise TurnstileSolverError(f"Task creation failed: {error_msg}")
            
            task_id = task_data["taskId"]
            logger.debug(f"Task created with ID: {task_id}")
            
            # Poll for result with proper polling endpoint
            result = await self._poll_for_result(task_id)
            return result
            
        except requests.RequestException as e:
            raise NetworkError(f"Network error during remote token acquisition: {str(e)}")
        except json.JSONDecodeError as e:
            raise TurnstileSolverError(f"Invalid JSON response from remote solver: {str(e)}")
    
    async def _poll_for_result(self, task_id: str) -> str:
        """
        Poll the remote solver for task completion
        
        Args:
            task_id: Task ID to poll
            
        Returns:
            Turnstile token when task completes
            
        Raises:
            TurnstileSolverError: If polling fails or task fails
            TimeoutError: If task doesn't complete within timeout
        """
        max_attempts = self.config.timeout // 2  # Poll every 2 seconds
        poll_url = f"{self.config.remote_endpoint.rstrip('/')}/getTaskResult"
        
        for attempt in range(max_attempts):
            try:
                response = self.session.post(
                    poll_url,
                    json={"taskId": task_id},
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    timeout=self.config.timeout,
                )
                
                if response.status_code != 200:
                    logger.warning(f"Unexpected status {response.status_code} polling task {task_id}")
                    await asyncio.sleep(2)
                    continue
                
                try:
                    data = response.json()
                except json.JSONDecodeError:
                    # Not JSON response means task probably not ready
                    await asyncio.sleep(2)
                    continue

                if data.get("errorId", 0) != 0:
                    error = data.get("errorDescription", "Unknown error")
                    raise TurnstileSolverError(f"Task failed: {error}")
                
                if data.get("status") == "ready":
                    solution = data.get("solution", {})
                    token = solution.get("token")
                    if token:
                        return token
                    else:
                        raise TurnstileSolverError("Task completed but no token in solution")
                
                elif data.get("status") == "processing":
                    logger.debug(f"Task {task_id} still processing (attempt {attempt + 1}/{max_attempts})")
                    await asyncio.sleep(2)
                    continue
                
                elif data.get("status") == "failed":
                    error = data.get("error", "Unknown error")
                    raise TurnstileSolverError(f"Task failed: {error}")
                
                else:
                    logger.debug(f"Task {task_id} status: {data.get('status', 'unknown')}")
                    await asyncio.sleep(2)
                    
            except requests.RequestException as e:
                logger.warning(f"Network error polling task {task_id}: {str(e)}")
                await asyncio.sleep(2)
        
        raise TimeoutError(
            f"Task {task_id} did not complete within {self.config.timeout} seconds",
            timeout_seconds=self.config.timeout,
            operation="turnstile_solver_poll"
        )
    
    async def _get_local_token(
        self,
        website_url: str,
        website_key: str,
        action: Optional[str] = None,
        data: Optional[str] = None,
        pagedata: Optional[str] = None,
        user_agent: Optional[str] = None
    ) -> str:
        """
        Get token from local solver process
        
        Args:
            website_url: URL of the website
            website_key: Turnstile sitekey  
            action: Turnstile action parameter
            data: Turnstile data parameter
            pagedata: Turnstile pagedata parameter
            user_agent: User-Agent string
            
        Returns:
            Turnstile token string
            
        Raises:
            TurnstileSolverError: If local solver fails
        """
        if not self.config.local_solver_path:
            raise TurnstileSolverError("Local solver path not configured")
            
        raise TurnstileSolverError("Local solver not yet implemented")
    
    async def _get_mock_token(self) -> str:
        """
        Generate a mock token for testing purposes
        
        Returns:
            Mock Turnstile token
        """
        logger.warning("Using mock Turnstile token - suitable for testing only")
        
        # Simulate solving time
        await asyncio.sleep(1)
        
        # Generate a mock token
        import secrets
        mock_token = f"mock_{secrets.token_urlsafe(256)}"
        
        return mock_token
    
    async def health_check(self) -> Dict[str, Any]:
        """
        Perform health check on the solver service
        
        Returns:
            Health status information
        """
        health_info = {
            "solver_type": self.config.solver_type.value,
            "configured": True,
            "timestamp": datetime.now().isoformat()
        }
        
        if self.config.solver_type == TurnstileSolverType.REMOTE:
            try:
                # Try to make a simple request to check connectivity
                response = self.session.get(
                    f"{self.config.remote_endpoint.rstrip('/')}/",
                    timeout=10
                )
                health_info["remote_endpoint_reachable"] = response.status_code < 400
                health_info["status_code"] = response.status_code
            except Exception as e:
                health_info["remote_endpoint_reachable"] = False
                health_info["error"] = str(e)
        
        return health_info
    
    def close(self) -> None:
        """Clean up resources"""
        self.session.close()
        logger.debug("TurnstileSolver closed")
    
    async def __aenter__(self):
        """Async context manager entry"""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        self.close()
