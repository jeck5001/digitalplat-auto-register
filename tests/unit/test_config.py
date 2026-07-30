"""
Unit tests for configuration management
"""

import pytest
from unittest.mock import patch, mock_open

from digitalplat_auto_register.core.config import ConfigManager, get_config
from digitalplat_auto_register.types import DigitalPlatConfig, TurnstileSolverType, EmailProvider
from digitalplat_auto_register.exceptions import ConfigurationError


class TestConfigManager:
    """Test cases for ConfigManager class"""
    
    def test_load_from_dict(self, config_manager):
        """Test loading configuration from dictionary"""
        test_config = {
            "base_url": "https://test.example.com",
            "turnstile": {
                "solver_type": "mock"
            }
        }
        
        config = config_manager.load_from_dict(test_config).load()
        
        assert config.base_url == "https://test.example.com"
        assert config.turnstile.solver_type == TurnstileSolverType.MOCK
    
    def test_load_from_env(self, config_manager, monkeypatch):
        """Test loading configuration from environment variables"""
        # Set test environment variables
        monkeypatch.setenv("DIGITALPLAT_BASE_URL", "https://env.test.com")
        monkeypatch.setenv("TURNSTILE_SOLVER_TYPE", "mock")
        monkeypatch.setenv("EMAIL_PROVIDER", "mail.td")
        monkeypatch.setenv("BROWSER_HEADLESS", "false")
        
        config = config_manager.load_from_env().load()
        
        assert config.base_url == "https://env.test.com"
        assert config.turnstile.solver_type == TurnstileSolverType.MOCK
        assert config.email.provider == EmailProvider.MAIL_TD
        assert config.browser.headless == False
    
    @pytest.mark.parametrize("file_format", ["yaml", "json"])
    def test_load_from_file(self, config_manager, temp_config_file, file_format):
        """Test loading configuration from file"""
        # Convert to different format if needed
        if file_format == "json":
            import json
            import yaml
            
            # Read YAML and convert to JSON
            with open(temp_config_file, 'r') as f:
                yaml_data = yaml.safe_load(f)
            
            json_file = temp_config_file.with_suffix('.json')
            with open(json_file, 'w') as f:
                json.dump(yaml_data, f)
            
            config_file = json_file
        else:
            config_file = temp_config_file
        
        config = config_manager.load_from_file(str(config_file)).load()
        
        assert config.base_url == "https://test.digitalplat.org"
        assert config.turnstile.solver_type == TurnstileSolverType.MOCK
        assert config.email.provider.value == "mock"
    
    def test_merge_configs_priority(self, config_manager):
        """Test configuration merging with proper precedence"""
        # Load base config
        base_config = {
            "base_url": "https://base.example.com",
            "turnstile": {"solver_type": "local"}
        }
        config_manager.load_from_dict(base_config)
        
        # Override with higher priority config
        override_config = {
            "base_url": "https://override.example.com",
            "turnstile": {"solver_type": "remote"}
        }
        
        config = config_manager.load_from_dict(override_config).load()
        
        # Highest priority should win
        assert config.base_url == "https://override.example.com"
        assert config.turnstile.solver_type == TurnstileSolverType.REMOTE
    
    def test_load_no_sources_raises_error(self, config_manager):
        """Test that loading with no sources raises appropriate error"""
        with pytest.raises(ConfigurationError, match="No configuration sources loaded"):
            config_manager.load()
    
    def test_config_validation(self, config_manager):
        """Test configuration validation"""
        invalid_config = {
            "base_url": "invalid-url",  # Invalid URL format
        }
        
        with pytest.raises(Exception):  # Pydantic validation error
            config_manager.load_from_dict(invalid_config).load()


class TestConfigManagerSave:
    """Test configuration saving functionality"""
    
    def test_save_to_yaml(self, config_manager, tmp_path):
        """Test saving configuration to YAML file"""
        test_config = {"base_url": "https://test.example.com"}
        
        # Load and save
        config = config_manager.load_from_dict(test_config).load()
        
        output_file = tmp_path / "test_output.yaml"
        config_manager.save_to_file(str(output_file), format="yaml")
        
        # Verify file was created and contains expected data
        assert output_file.exists()
        
        import yaml
        with open(output_file, 'r') as f:
            saved_config = yaml.safe_load(f)
        
        assert saved_config["base_url"] == "https://test.example.com"
    
    def test_save_to_json(self, config_manager, tmp_path):
        """Test saving configuration to JSON file"""
        test_config = {"base_url": "https://test.example.com"}
        
        # Load and save
        config = config_manager.load_from_dict(test_config).load()
        
        output_file = tmp_path / "test_output.json"
        config_manager.save_to_file(str(output_file), format="json")
        
        # Verify file was created and contains expected data
        assert output_file.exists()
        
        import json
        with open(output_file, 'r') as f:
            saved_config = json.load(f)
        
        assert saved_config["base_url"] == "https://test.example.com"


class TestGetConfigFunction:
    """Test the global get_config function"""
    
    def test_get_config_without_loading(self):
        """Test that get_config raises error when no config loaded"""
        from digitalplat_auto_register.core.config import _config_manager
        
        # Reset the global config manager
        _config_manager.config = None
        
        with pytest.raises(ConfigurationError, match="No configuration loaded"):
            get_config()
    
    def test_get_config_after_loading(self, config_manager):
        """Test getting config after loading"""
        test_config = {"base_url": "https://test.example.com"}
        config = config_manager.load_from_dict(test_config).load()
        
        # Import to get the function that uses the global manager
        from digitalplat_auto_register.core.config import get_config as gc2
        
        # This should work since we loaded the config
        retrieved_config = gc2()
        
        assert retrieved_config.base_url == "https://test.example.com"