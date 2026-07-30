"""
Configuration management module for DigitalPlat Auto Register

This module handles loading, validation, and management of configuration
from various sources including environment variables, configuration files,
and programmatic settings.
"""

import os
import json
import yaml
from pathlib import Path
from typing import Optional, Dict, Any

from dotenv import load_dotenv
from loguru import logger

from ..types import DigitalPlatConfig, EmailProvider, TurnstileSolverType
from ..exceptions import ConfigurationError


class ConfigManager:
    """
    Configuration manager for DigitalPlat Auto Register package.
    
    Handles loading configuration from multiple sources:
    - Environment variables (.env files, system environment)
    - Configuration files (JSON, YAML, TOML)
    - Programmatic defaults
    """
    
    def __init__(self):
        """Initialize the configuration manager"""
        self.config: Optional[DigitalPlatConfig] = None
        self.config_sources: Dict[str, Any] = {}
        
    def load_from_env(self, env_file: Optional[str] = None) -> 'ConfigManager':
        """
        Load configuration from environment variables
        
        Args:
            env_file: Path to .env file. If None, will look for .env in current directory
            
        Returns:
            Self for method chaining
        """
        try:
            if env_file:
                if not Path(env_file).exists():
                    logger.warning(f"Environment file not found: {env_file}")
                else:
                    load_dotenv(env_file)
            else:
                # Try to load from current directory
                load_dotenv()
                
            # Extract configuration from environment variables
            env_config = {}
            
            # Base URLs
            if os.environ.get('DIGITALPLAT_BASE_URL'):
                env_config['base_url'] = os.environ['DIGITALPLAT_BASE_URL']
                
            # Turnstile configuration
            turnstile_config = {}
            if os.environ.get('TURNSTILE_SOLVER_TYPE'):
                turnstile_config['solver_type'] = TurnstileSolverType(os.environ['TURNSTILE_SOLVER_TYPE'])
            if os.environ.get('TURNSTILE_REMOTE_ENDPOINT'):
                turnstile_config['remote_endpoint'] = os.environ['TURNSTILE_REMOTE_ENDPOINT']
            if os.environ.get('TURNSTILE_SITEKEY'):
                turnstile_config['sitekey'] = os.environ['TURNSTILE_SITEKEY']
            if os.environ.get('TURNSTILE_TIMEOUT'):
                turnstile_config['timeout'] = int(os.environ['TURNSTILE_TIMEOUT'])
            if turnstile_config:
                env_config['turnstile'] = turnstile_config
                
            # Email configuration
            email_config = {}
            if os.environ.get('EMAIL_PROVIDER'):
                email_config['provider'] = EmailProvider(os.environ['EMAIL_PROVIDER'])
            if os.environ.get('EMAIL_TIMEOUT'):
                email_config['timeout'] = int(os.environ['EMAIL_TIMEOUT'])
            if email_config:
                env_config['email'] = email_config
                
            # Browser configuration
            browser_config = {}
            if os.environ.get('BROWSER_ENGINE'):
                browser_config['engine'] = os.environ['BROWSER_ENGINE']
            if os.environ.get('BROWSER_HEADLESS'):
                browser_config['headless'] = os.environ['BROWSER_HEADLESS'].lower() == 'true'
            if os.environ.get('BROWSER_TIMEOUT'):
                browser_config['timeout'] = int(os.environ['BROWSER_TIMEOUT'])
            if os.environ.get('BROWSER_WAIT_FOR_TIMEOUT'):
                browser_config['wait_for_timeout'] = int(os.environ['BROWSER_WAIT_FOR_TIMEOUT'])
            if browser_config:
                env_config['browser'] = browser_config
                
            # Proxy configuration
            proxy_config = {}
            if os.environ.get('PROXY_ENABLED'):
                proxy_config['enabled'] = os.environ['PROXY_ENABLED'].lower() == 'true'
            if os.environ.get('PROXY_SERVER'):
                proxy_config['server'] = os.environ['PROXY_SERVER']
            if os.environ.get('PROXY_USERNAME'):
                proxy_config['username'] = os.environ['PROXY_USERNAME']
            if os.environ.get('PROXY_PASSWORD'):
                proxy_config['password'] = os.environ['PROXY_PASSWORD']
            if proxy_config:
                env_config['proxy'] = proxy_config
                
            # Logging configuration
            logging_config = {}
            if os.environ.get('LOG_LEVEL'):
                logging_config['level'] = os.environ['LOG_LEVEL']
            if os.environ.get('LOG_FILE'):
                logging_config['log_file'] = os.environ['LOG_FILE']
            if logging_config:
                env_config['logging'] = logging_config
                
            # Operation settings
            if os.environ.get('MAX_REGISTRATION_ATTEMPTS'):
                env_config['max_registration_attempts'] = int(os.environ['MAX_REGISTRATION_ATTEMPTS'])
            if os.environ.get('VERIFICATION_TIMEOUT'):
                env_config['verification_timeout'] = int(os.environ['VERIFICATION_TIMEOUT'])
            if os.environ.get('VERIFICATION_CHECK_INTERVAL'):
                env_config['verification_check_interval'] = int(os.environ['VERIFICATION_CHECK_INTERVAL'])
                
            self.config_sources['environment'] = env_config
            logger.debug("Loaded configuration from environment variables")
            
        except Exception as e:
            raise ConfigurationError(f"Failed to load configuration from environment: {str(e)}")
            
        return self
    
    def load_from_file(self, file_path: str) -> 'ConfigManager':
        """
        Load configuration from a file (JSON, YAML, or TOML)
        
        Args:
            file_path: Path to configuration file
            
        Returns:
            Self for method chaining
        """
        try:
            path = Path(file_path)
            if not path.exists():
                raise ConfigurationError(f"Configuration file not found: {file_path}")
                
            file_config = {}
            
            if path.suffix.lower() in ['.json']:
                with open(path, 'r', encoding='utf-8') as f:
                    file_config = json.load(f)
            elif path.suffix.lower() in ['.yaml', '.yml']:
                with open(path, 'r', encoding='utf-8') as f:
                    file_config = yaml.safe_load(f)
            elif path.suffix.lower() in ['.toml']:
                try:
                    import tomllib
                    with open(path, 'rb') as f:
                        file_config = tomllib.load(f)
                except ImportError:
                    try:
                        import toml
                        with open(path, 'r', encoding='utf-8') as f:
                            file_config = toml.load(f)
                    except ImportError:
                        raise ConfigurationError("TOML support requires 'tomllib' (Python 3.11+) or 'toml' package")
            else:
                raise ConfigurationError(f"Unsupported configuration file format: {path.suffix}")
                
            self.config_sources['file'] = file_config
            logger.debug(f"Loaded configuration from file: {file_path}")
            
        except Exception as e:
            raise ConfigurationError(f"Failed to load configuration from file {file_path}: {str(e)}")
            
        return self
    
    def load_from_dict(self, config_dict: Dict[str, Any]) -> 'ConfigManager':
        """
        Load configuration from a dictionary
        
        Args:
            config_dict: Configuration dictionary
            
        Returns:
            Self for method chaining
        """
        try:
            self.config_sources['dict'] = config_dict
            logger.debug("Loaded configuration from dictionary")
        except Exception as e:
            raise ConfigurationError(f"Failed to load configuration from dictionary: {str(e)}")
            
        return self
    
    def merge_configs(self) -> DigitalPlatConfig:
        """
        Merge all loaded configuration sources with proper precedence
        
        Precedence (highest to lowest):
        1. Dictionary configuration
        2. File configuration  
        3. Environment configuration
        4. Default configuration
        
        Returns:
            Merged DigitalPlatConfig instance
        """
        try:
            # Start with default configuration
            merged_config = {}
            
            # Apply configurations in order of precedence
            for source in ['environment', 'file', 'dict']:
                if source in self.config_sources:
                    self._deep_merge(merged_config, self.config_sources[source])
                    
            # Create and validate the final configuration
            self.config = DigitalPlatConfig(**merged_config)
            
            logger.info("Configuration loaded and validated successfully")
            return self.config
            
        except Exception as e:
            raise ConfigurationError(f"Failed to merge and validate configuration: {str(e)}")
    
    def _deep_merge(self, target: Dict[str, Any], source: Dict[str, Any]) -> None:
        """
        Deep merge source dictionary into target dictionary
        
        Args:
            target: Target dictionary to merge into
            source: Source dictionary to merge from
        """
        for key, value in source.items():
            if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                self._deep_merge(target[key], value)
            else:
                target[key] = value
    
    def load(self) -> DigitalPlatConfig:
        """
        Load and merge configuration from all sources
        
        Returns:
            Validated DigitalPlatConfig instance
        """
        if not self.config_sources:
            raise ConfigurationError("No configuration sources loaded. Use load_from_* methods first.")
            
        return self.merge_configs()
    
    def save_to_file(self, file_path: str, format: str = 'yaml') -> None:
        """
        Save current configuration to a file
        
        Args:
            file_path: Path where to save the configuration
            format: Format to save as ('json', 'yaml', 'toml')
        """
        if self.config is None:
            raise ConfigurationError("No configuration to save. Load configuration first.")
            
        try:
            path = Path(file_path)
            config_dict = self.config.dict(exclude_none=True)
            
            if format.lower() == 'json':
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(config_dict, f, indent=2, ensure_ascii=False)
                    
            elif format.lower() in ['yaml', 'yml']:
                with open(path, 'w', encoding='utf-8') as f:
                    yaml.safe_dump(config_dict, f, indent=2, allow_unicode=True)
                    
            elif format.lower() == 'toml':
                try:
                    import tomli_w
                    with open(path, 'wb') as f:
                        tomli_w.dump(config_dict, f)
                except ImportError:
                    try:
                        import toml
                        with open(path, 'w', encoding='utf-8') as f:
                            toml.dump(config_dict, f)
                    except ImportError:
                        raise ConfigurationError("TOML support requires 'tomli_w' or 'toml' package")
            else:
                raise ConfigurationError(f"Unsupported format: {format}")
                
            logger.info(f"Configuration saved to {file_path} as {format.upper()}")
            
        except Exception as e:
            raise ConfigurationError(f"Failed to save configuration to {file_path}: {str(e)}")
    
    def get_config(self) -> DigitalPlatConfig:
        """
        Get the current configuration
        
        Returns:
            Current DigitalPlatConfig instance
            
        Raises:
            ConfigurationError: If no configuration has been loaded
        """
        if self.config is None:
            raise ConfigurationError("No configuration loaded. Call load() first.")
        return self.config


# Create a global configuration manager instance
_config_manager = ConfigManager()


def get_config() -> DigitalPlatConfig:
    """
    Get the global configuration instance
    
    Returns:
        Current DigitalPlatConfig instance
        
    Raises:
        ConfigurationError: If no configuration has been loaded
    """
    return _config_manager.get_config()


def load_config(*args, **kwargs) -> DigitalPlatConfig:
    """
    Convenience function to load configuration from single source
    
    Args:
        *args, **kwargs: Arguments passed to ConfigManager
        
    Returns:
        Loaded DigitalPlatConfig instance
    """
    return _config_manager.load(*args, **kwargs)
