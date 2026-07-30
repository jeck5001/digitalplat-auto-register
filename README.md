# DigitalPlat Auto Register

<div align="center">

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![version](https://img.shields.io/badge/version-0.1.0-blue)](https://github.com/your-repo/digitalplat-auto-register)

**Automated DigitalPlat domain registration with temporary email verification and Cloudflare Turnstile bypass**

*[English](#english) | [中文](#中文)*

</div>


# English

## 🎯 Overview

DigitalPlat Auto Register is a powerful Python tool that automates the process of creating DigitalPlat accounts for domain management. It handles the entire registration workflow including:

- ✅ **Cloudflare Turnstile bypass** - Automatically obtains valid tokens
- ✅ **Temporary email integration** - Uses mail.td for email verification
- ✅ **Browser automation** - Fully automated form filling and submission
- ✅ **Email code retrieval** - Automatically extracts verification codes
- ✅ **Retry mechanisms** - Robust error handling and recovery

## 🚀 Quick Start

### Installation

```bash
pip install digitalplat-auto-register
```

### Basic Usage

```python
from digitalplat_auto_register import DigitalPlatRegistrar

# Create registrar
registrar = DigitalPlatRegistrar()

# Register account
result = await registrar.register_account(
    username="myuser",
    fullname="My Name", 
    phone="+1-555-123-4567",
    referral_code="abc123"
)

if result.success:
    print(f"Account {result.username} created successfully!")
    print(f"Email: {result.email}")
```

### Command Line Interface

```bash
# Single registration
digitalplat-register single --referral-code=abc123

# Batch registration from file
digitalplat-register batch registrations.json

# Generate sample config
digitalplat-register config generate > config.yaml
```

## 📋 Features

### Core Functionality
- **Turnstile Token Acquisition**: Automatically bypasses Cloudflare Turnstile verification using remote solver services
- **Temporary Email Management**: Creates and manages temporary email addresses using mail.td
- **Form Automation**: Fills registration forms with user data and handles submission
- **Email Verification**: Monitors email inbox and retrieves verification codes automatically
- **Popup Handling**: Interacts with verification popups and completes the verification process

### Advanced Features
- **Retry Logic**: Intelligent retry mechanisms with backoff for failed operations
- **Progress Tracking**: Real-time progress monitoring with detailed step information
- **Error Recovery**: Comprehensive error handling and recovery strategies
- **Logging System**: Detailed logging with configurable output
- **Configuration Management**: Flexible configuration via files, environment variables, or programmatic settings
- **CLI Interface**: Full-featured command-line interface for automation

### Supported Email Providers
- **mail.td** - Primary provider with browser automation
- **10minutemail** - Planned integration
- **Guerrilla Mail** - Planned integration

## 📦 Installation

### Prerequisites
- Python 3.8 or higher
- Internet connection
- Remote Turnstile solver service access (if using remote solver)

### Using pip

```bash
pip install digitalplat-auto-register
```

### From source

```bash
git clone https://github.com/your-repo/digitalplat-auto-register.git
cd digitalplat-auto-register
pip install -e .
```

### Development Installation

```bash
pip install -e ".[dev]"
playwright install chromium
```

## 🛠️ Configuration

### Configuration File (config.yaml)

```yaml
base_url: https://dash.domain.digitalplat.org
registration_endpoint: /auth/register

turnstile:
  enabled: true
  solver_type: remote
  remote_endpoint: http://192.168.5.35:5072
  sitekey: 0x4AAAAAAAxuMrGCYFcOwd1N
  timeout: 120
  max_retries: 3

email:
  provider: mail.td
  timeout: 300

browser:
  headless: true
  timeout: 30000
  viewport_width: 1920
  viewport_height: 1080

proxy:
  enabled: false
  server: http://proxy.example.com:8080

logging:
  enabled: true
  level: INFO
  log_file: digitalplat_register.log

max_registration_attempts: 3
verification_timeout: 300
verification_check_interval: 5
```

### Environment Variables

```bash
export DIGITALPLAT_BASE_URL="https://dash.domain.digitalplat.org"
export TURNSTILE_SOLVER_TYPE="remote"
export TURNSTILE_REMOTE_ENDPOINT="http://192.168.5.35:5072"
export EMAIL_PROVIDER="mail.td"
export BROWSER_HEADLESS="true"
```

## 💻 API Usage

### Basic Registration

```python
import asyncio
from digitalplat_auto_register import register_with_defaults

async def main():
    result = await register_with_defaults(
        username="testuser_123",
        fullname="Test User",
        phone="+1-555-123-4567",
        referral_code="ref123"
    )
    
    if result.success:
        print(f"✅ Registration successful: {result.username}")
    else:
        print(f"❌ Registration failed: {result.error}")
        print(f"   Failed stage: {result.error_stage}")

asyncio.run(main())
```

### Advanced Usage with Custom Configuration

```python
from digitalplat_auto_register.types import DigitalPlatConfig
from digitalplat_auto_register.core.registrar import DigitalPlatRegistrar

# Create custom configuration
config = DigitalPlatConfig(
    base_url="https://dash.domain.digitalplat.org",
    turnstile={
        "solver_type": "remote",
        "remote_endpoint": "http://192.168.5.35:5072",
        "timeout": 120
    },
    email={
        "provider": "mail.td"
    },
    browser={
        "headless": True
    }
)

# Create registrar with custom config
registrar = DigitalPlatRegistrar(config)

# Register with progress callback
def progress_callback(step_result):
    print(f"Step: {step_result.name} - {step_result.status}")

result = await registrar.register_account(
    username="custom_user",
    referral_code="abc123",
    on_step_complete=progress_callback
)
```

### Batch Registration

```python
import asyncio
from digitalplat_auto_register.core.registrar import DigitalPlatRegistrar

async def batch_register():
    registrar = DigitalPlatRegistrar()
    
    registration_data = [
        {"username": f"user_{i}", "fullname": f"User {i}"} 
        for i in range(5)
    ]
    
    results = []
    for data in registration_data:
        result = await registrar.register_account(**data)
        results.append(result)
        await asyncio.sleep(5)  # Delay between registrations
    
    successful = sum(1 for r in results if r.success)
    print(f"Registered {successful}/{len(results)} accounts successfully")

asyncio.run(batch_register())
```

## 🔧 CLI Reference

### Commands

```bash
# Show help
digitalplat-register --help

# Register single account
digitalplat-register single \
  --username="myuser" \
  --referral-code="abc123" \
  --config="config.yaml" \
  --verbose-steps

# Batch registration
digitalplat-register batch registrations.json \
  --config="config.yaml" \
  --output-dir="results/"

# Generate configuration file
digitalplat-register config generate --format=yaml > config.yaml

# Show version
digitalplat-register version
```

### Batch File Format (JSON)

```json
[
  {
    "username": "user1",
    "fullname": "User One",
    "phone": "+1-555-123-4567",
    "referral_code": "abc123"
  },
  {
    "username": "user2", 
    "fullname": "User Two",
    "referral_code": "xyz789"
  }
]
```

## 🏗️ Architecture

### Core Components

1. **DigitalPlatRegistrar** - Main orchestration class
2. **TurnstileSolver** - Token acquisition service
3. **EmailService** - Temporary email management
4. **BrowserAutomationService** - Playwright-based browser control
5. **ConfigManager** - Configuration management
6. **RegistrationResult** - Comprehensive result tracking

### Workflow

1. **Configuration Loading**: Load settings from files/env/programmatic sources
2. **Service Initialization**: Initialize Turnstile solver, email service, and browser
3. **Turnstile Token**: Acquire valid Cloudflare Turnstile token
4. **Email Creation**: Generate temporary email address
5. **Form Navigation**: Navigate to registration page
6. **Form Filling**: Populate form fields with user data
7. **Token Injection**: Inject Turnstile token into form
8. **Form Submission**: Submit registration form
9. **Email Monitoring**: Wait for and check verification email
10. **Code Extraction**: Extract verification code from email content
11. **Verification**: Handle verification popup and complete verification
12. **Result Reporting**: Track and report all steps and results

## 🧪 Testing

Run the test suite:

```bash
# Run all tests
pytest

# Run unit tests only
pytest tests/unit/

# Run with coverage
pytest --cov=digitalplat_auto_register --cov-report=html
```

## 📄 License

This project is licensed under the MIT License. See [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- [Playwright](https://playwright.dev/) for browser automation
- [Cloudflare Turnstile](https://www.cloudflare.com/products/turnstile/) for CAPTCHA protection
- [mail.td](https://mail.td) for temporary email service

---

# 中文

## 🎯 项目概述

DigitalPlat Auto Register 是一个功能强大的 Python 工具，用于自动化创建和管理 DigitalPlat 域名账户。它处理完整的注册流程，包括：

- ✅ **Cloudflare Turnstile 验证绕过** - 自动获取有效令牌
- ✅ **临时邮箱集成** - 使用 mail.td 进行邮箱验证
- ✅ **浏览器自动化** - 全自动填写和提交表单
- ✅ **邮件验证码获取** - 自动提取验证码
- ✅ **重试机制** - 健壮的错误处理和恢复

## 🚀 快速开始

### 安装

```bash
pip install digitalplat-auto-register
```

### 基本使用

```python
from digitalplat_auto_register import DigitalPlatRegistrar

# 创建注册器
registrar = DigitalPlatRegistrar()

# 注册账户
result = await registrar.register_account(
    username="我的用户",
    fullname="我的姓名", 
    phone="+86-138-1234-5678",
    referral_code="邀请码"
)

if result.success:
    print(f"账户 {result.username} 创建成功！")
    print(f"邮箱: {result.email}")
```

## 📦 安装要求

- Python 3.8 或更高版本
- 网络连接
- Turnstile 解算服务访问权限（如果使用远程解算器）

## 🔧 配置说明

支持通过以下方式进行配置：
- YAML/JSON/TOML 配置文件
- 环境变量
- 编程方式

## 📚 文档资源

- [详细安装指南](docs/installation.md)
- [API 参考](docs/api.md)  
- [配置说明](docs/configuration.md)
- [使用示例](docs/examples.md)
- [贡献指南](docs/contributing.md)

---

<div align="center">
**⭐ Star this repo if it helps you!**  
**Issues and PRs are welcome!**
</div>