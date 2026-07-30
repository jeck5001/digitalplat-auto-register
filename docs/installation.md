# Installation Guide

This guide provides detailed instructions for installing and setting up DigitalPlat Auto Register.

## Prerequisites

Before installing, ensure you have:

- Python 3.8 or higher
- pip package manager
- Internet connection
- Optional: Remote Turnstile solver service access

## Installation Methods

### Method 1: Install from PyPI (Recommended)

```bash
pip install digitalplat-auto-register
```

### Method 2: Install from source

```bash
# Clone the repository
git clone https://github.com/your-repo/digitalplat-auto-register.git
cd digitalplat-auto-register

# Install in development mode
pip install -e .
```

### Method 3: Install with development dependencies

```bash
pip install -e ".[dev]"
```

### Method 4: Manual installation from requirements

```bash
# Clone repository
git clone https://github.com/your-repo/digitalplat-auto-register.git
cd digitalplat-auto-register

# Install requirements
pip install -r requirements.txt
pip install -e .
```

## Post-Installation Setup

### Install Playwright Browsers

If you're using the browser automation features, install the required browsers:

```bash
playwright install chromium
```

### Verify Installation

Test that the package is installed correctly:

```python
python -c "import digitalplat_auto_register; print('Installation successful')"
```

Test the CLI:

```bash
digitalplat-register --help
```

## Configuration Setup

### Create Configuration File

```bash
# Generate sample configuration
digitalplat-register config generate --format=yaml > config.yaml
```

### Environment Variables

Set up environment variables for sensitive information:

```bash
export TURNSTILE_REMOTE_ENDPOINT="http://your-solver:port"
export EMAIL_PROVIDER="mail.td"
export LOG_LEVEL="INFO"
```

## Troubleshooting

### Common Issues

#### 1. Playwright Installation Issues

**Error**: `ModuleNotFoundError: No module named 'playwright'`

**Solution**:
```bash
pip install playwright
playwright install chromium
```

#### 2. Missing Dependencies

**Error**: Various import errors

**Solution**:
```bash
pip install -e ".[dev]"  # Install with all dependencies
```

#### 3. Browser Launch Issues

**Error**: Browser fails to start

**Solution**:
1. Ensure Playwright is installed correctly
2. Try running in headed mode: `export BROWSER_HEADLESS=false`
3. Check system requirements

#### 4. Turnstile Solver Connection Issues

**Error**: Cannot connect to remote solver

**Solution**:
1. Verify solver endpoint URL
2. Check network connectivity
3. Ensure solver service is running
4. Configure proper timeout settings

#### 5. Email Service Issues

**Error**: Cannot receive verification emails

**Solution**:
1. Verify email provider settings
2. Check internet connectivity
3. Ensure email service is accessible
4. Adjust timeout settings if needed

### Debug Mode

Enable debug logging to troubleshoot issues:

```python
import os
os.environ['LOG_LEVEL'] = 'DEBUG'

# Or via command line
digitalplat-register single --verbose --referral-code=abc123
```

### Check System Requirements

Verify your system meets requirements:

```python
import platform
import sys

print(f"Python version: {sys.version}")
print(f"Platform: {platform.platform()}")
print(f"Architecture: {platform.machine()}")
```

## Development Setup

If you plan to contribute or modify the code:

### Clone Repository

```bash
git clone https://github.com/your-repo/digitalplat-auto-register.git
cd digitalplat-auto-register
```

### Install Development Dependencies

```bash
pip install -e ".[dev]"
```

### Set Up Pre-commit Hooks

```bash
pip install pre-commit
pre-commit install
```

### Run Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=digitalplat_auto_register --cov-report=html

# Run specific test category
pytest tests/unit/
pytest tests/integration/
```

### Code Formatting

```bash
# Format code
black src/
black tests/

# Sort imports
isort src/ tests/

# Type checking
mypy src/
```

## Docker Setup (Optional)

If you prefer to run in Docker:

### Build Docker Image

```dockerfile
FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN pip install -e .

CMD ["digitalplat-register", "--help"]
```

### Build and Run

```bash
docker build -t digitalplat-register .
docker run digitalplat-register --help
```

## Next Steps

After successful installation:

1. [Configure your settings](configuration.md)
2. [Try the basic examples](examples.md)
3. [Explore the API reference](api.md)
4. [Check out advanced usage](advanced.md)

## Getting Help

If you encounter issues:

- Check the [troubleshooting](#troubleshooting) section
- Review [existing issues](https://github.com/your-repo/digitalplat-auto-register/issues)
- Create a [new issue](https://github.com/your-repo/digitalplat-auto-register/issues/new) if needed

## License and Legal Notice

This tool is provided for educational and research purposes. Ensure you comply with the terms of service of any websites you interact with and applicable laws in your jurisdiction.

By using this software, you acknowledge that automated interaction with websites may be against their terms of service. Use responsibly and at your own risk.