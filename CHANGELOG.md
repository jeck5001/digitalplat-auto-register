# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-07-30

### Added
- Initial project release
- DigitalPlat registration automation functionality
- Turnstile token acquisition service
- Email service integration (mail.td)
- Browser automation with Playwright
- Configuration management system
- CLI interface with multiple commands
- Comprehensive error handling and retry mechanisms
- Logging and progress tracking
- Unit and integration test suite
- Complete documentation

### Core
- `DigitalPlatRegistrar` - Main orchestration class
- `TurnstileSolver` - Cloudflare Turnstile bypass
- `EmailService` - Temporary email management
- `BrowserAutomationService` - Playwright browser control
- `ConfigManager` - Flexible configuration handling

### Features
- Single account registration
- Batch registration support  
- Progress callbacks
- Step-by-step result tracking
- Comprehensive error reporting
- Screenshot capture
- Console logging and file logging
- JSON/YAML configuration support
- Environment variable configuration
- Retry logic with exponential backoff

### CLI Commands
- `single` - Register single account
- `batch` - Batch registeration from file
- `config-generate` - Generate sample configuration
- `version` - Show version information

### Services Supported
- **Email Providers**: mail.td (primary), others planned
- **Turnstile Solvers**: Remote HTTP API, Local processes, Mock for testing
- **Browsers**: Chromium via Playwright

### Configuration
- YAML/JSON/TOML configuration files
- Environment variable support
- Programmatic configuration API
- Sensible defaults for all settings

### Testing
- Unit tests for core functionality
- Integration test framework
- Mock services for testing
- Coverage reporting setup

### Documentation
- Comprehensive README with examples
- Installation guide
- API reference
- Configuration documentation
- Contributing guidelines

## [Unreleased]

### Planned
- Support for additional email providers (10minutemail, Guerrilla Mail)
- Enhanced error recovery strategies
- Docker deployment option
- API for programmatic usage
- Web interface option
- Monitoring and alerting integration
- Database storage of registration results

### Security
- Secure credential handling
- Improved error message sanitization

### Performance
- Concurrent registration support
- Improved browser resource management
- Caching optimizations
