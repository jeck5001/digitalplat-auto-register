#!/usr/bin/env python3
"""
DigitalPlat Auto Registration Tool Setup

A Python package for automating DigitalPlat domain registration using temporary email
services and Cloudflare Turnstile bypass techniques.

Author: Auto-generated
Version: 0.1.0
License: MIT
"""

from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

with open("requirements.txt", "r", encoding="utf-8") as fh:
    requirements = fh.read().strip().split('\n')

setup(
    name="digitalplat-auto-register",
    version="0.1.0",
    author="Auto-generated",
    author_email="auto@example.com",
    description="Automated DigitalPlat domain registration with temporary email verification",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/your-repo/digitalplat-auto-register",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Topic :: Internet :: WWW/HTTP :: Site Management",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
    python_requires=">=3.8",
    install_requires=requirements,
    extras_require={
        "dev": [
            "pytest>=7.0",
            "pytest-cov>=4.0",
            "httpx2>=0.1.0",
            "black>=22.0",
            "flake8>=5.0",
            "mypy>=0.991",
            "pre-commit>=2.20",
        ],
    },
    entry_points={
        "console_scripts": [
            "digitalplat-register=digitalplat_auto_register.cli:main",
            "digitalplat-register-web=digitalplat_auto_register.web_app:main",
        ],
    },
    include_package_data=True,
    zip_safe=False,
)
