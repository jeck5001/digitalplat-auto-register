# Contributing to DigitalPlat Auto Register

We welcome contributions from the community! This guide will help you get started.

## Code of Conduct

By participating in this project, you agree to maintain a respectful and inclusive environment for everyone.

## Getting Started

### Prerequisites

- Python 3.8+
- Git
- Basic understanding of asyncio and Playwright

### Development Setup

1. Fork and clone the repository:
   ```bash
   git clone https://github.com/your-username/digitalplat-auto-register.git
   cd digitalplat-auto-register
   ```

2. Install development dependencies:
   ```bash
   pip install -e ".[dev]"
   playwright install chromium
   ```

3. Set up pre-commit hooks:
   ```bash
   pre-commit install
   ```

## Contribution Workflow

### 1. Create an Issue

Before starting work, create an issue to:
- Report bugs
- Suggest new features
- Discuss changes
- Ask questions

### 2. Create a Branch

```bash
git checkout -b feature/your-feature-name
# or
git checkout -b fix/your-bug-fix
```

### 3. Make Changes

Follow our coding standards:
- Use descriptive commit messages
- Write tests for new functionality
- Update documentation as needed
- Follow existing code style

### 4. Run Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=digitalplat_auto_register --cov-report=html

# Run specific tests
pytest tests/unit/test_config.py
```

### 5. Format Code

```bash
# Format code
black src/ tests/

# Sort imports
isort src/ tests/

# Type check
mypy src/
```

### 6. Commit Changes

```bash
git add .
git commit -m "feat: add new feature description"
```

Good commit message format:
- Use present tense: "add feature" not "added feature"
- First line should be under 50 characters
- Use conventional commits format:
  - `feat:` new features
  - `fix:` bug fixes
  - `docs:` documentation changes
  - `test:` test additions/changes
  - `chore:` maintenance tasks
  - `style:` code formatting
  - `refactor:` code restructuring

### 7. Push and Create Pull Request

```bash
git push origin feature/your-feature-name
```

Then create a pull request on GitHub with:
- Clear description of changes
- Reference to related issues
- Screenshots if applicable

## Development Guidelines

### Code Style

- Follow PEP 8 and PEP 484
- Use type hints consistently
- Write docstrings for all public functions/classes
- Keep functions small and focused
- Use descriptive variable names

### Testing

- Write unit tests for all new functionality
- Maintain test coverage above 80%
- Use pytest conventions
- Test both success and failure cases
- Mock external dependencies in tests

### Documentation

- Update README.md for user-facing changes
- Update API documentation for code changes
- Add inline comments for complex logic
- Keep documentation examples up to date

### Error Handling

- Use specific exception types
- Provide helpful error messages
- Log errors appropriately
- Handle edge cases gracefully

### Performance

- Use asyncio best practices
- Minimize browser resource usage
- Implement proper cleanup
- Add timeouts to long-running operations

## Testing Guidelines

### Unit Tests

```python
def test_function_behavior():
    """Test specific function behavior"""
    # Arrange
    input_data = "test"
    
    # Act
    result = function_to_test(input_data)
    
    # Assert
    assert result == expected_output
```

### Integration Tests

```python
@pytest.mark.integration
async def test_registration_flow():
    """Test complete registration flow"""
    # Mock external services
    # Execute registration
    # Verify results
```

### Browser Tests

```python
@pytest.mark.browser
async def test_form_filling():
    """Test form filling functionality"""
    # Launch browser
    # Fill form
    # Verify fields populated
```

## Release Process

### Versioning

We use [Semantic Versioning](https://semver.org/):
- MAJOR version for incompatible API changes
- MINOR version for new functionality (backwards compatible)
- PATCH version for bug fixes (backwards compatible)

### Release Steps

1. Update CHANGELOG.md with release notes
2. Update version in `src/digitalplat_auto_register/__init__.py`
3. Create release commit: `git commit -m "release: v0.1.0"`
4. Create tag: `git tag v0.1.0`
5. Push changes: `git push origin main --tags`
6. Create GitHub release
7. Publish to PyPI (for maintainers)

## Pull Request Review Process

### What We Look For

- ✅ Code quality and style
- ✅ Test coverage and quality
- ✅ Documentation updates
- ✅ Appropriate error handling
- ✅ Performance considerations
- ✅ Security implications
- ✅ Backward compatibility

### Review Timeline

We aim to review pull requests within:
- 2-3 days for bug fixes
- 1-2 weeks for new features
- Longer for major changes (will communicate)

## Security

### Reporting Security Issues

If you discover a security vulnerability:

1. **Do not** create a public issue
2. **Do not** disclose until fixed
3. Contact maintainers privately
4. Provide detailed reproduction steps
5. Allow time for fix before disclosure

### Security Best Practices

- Validate all external inputs
- Sanitize error messages
- Use secure defaults
- Implement proper authentication checks
- Regular dependency updates

## Questions?

If you have questions about contributing:

1. Check existing GitHub issues and discussions
2. Read the documentation
3. Create a discussion thread
4. Reach out to maintainers

## License

By contributing to this project, you agree that your contributions will be licensed under the MIT License.

---

**Thank you for your contributions!** 🚀