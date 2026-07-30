"""
Unit tests for utility helper functions
"""

import pytest
from digitalplat_auto_register.utils.helpers import (
    generate_random_username,
    generate_password,
    generate_phone_number,
    extract_verification_code,
    parse_email_content,
    validate_email_address,
    chunk_list
)


class TestUsernameGeneration:
    """Test username generation utility"""
    
    def test_generate_random_username_default(self):
        """Test username generation with default parameters"""
        username = generate_random_username()
        
        assert isinstance(username, str)
        assert len(username) > 8  # At least prefix + timestamp + random part
        assert username.startswith("user_")
        
        # Should contain timestamp
        parts = username.split("_")
        assert len(parts) >= 3
    
    def test_generate_random_username_custom_prefix(self):
        """Test username generation with custom prefix"""
        username = generate_random_username(prefix="test")
        
        assert username.startswith("test_")
    
    def test_generate_random_username_custom_length(self):
        """Test username generation with custom length"""
        username = generate_random_username(length=12)
        
        # Extract random part (should be 12 chars)
        parts = username.split("_")
        random_part = parts[-1]
        assert len(random_part) == 12


class TestPasswordGeneration:
    """Test password generation utility"""
    
    def test_generate_password_default(self):
        """Test password generation with default parameters"""
        password = generate_password()
        
        assert isinstance(password, str)
        assert len(password) == 12
        
        # Should contain required character types
        assert any(c.isupper() for c in password), "Missing uppercase letter"
        assert any(c.islower() for c in password), "Missing lowercase letter"
        assert any(c.isdigit() for c in password), "Missing digit"
        assert any(c in "!@#$%^&*" for c in password), "Missing symbol"
    
    def test_generate_password_custom_length(self):
        """Test password generation with custom length"""
        password = generate_password(length=16)
        assert len(password) == 16
    
    def test_generate_password_no_symbols(self):
        """Test password generation without symbols"""
        password = generate_password(length=12, include_symbols=False)
        
        assert len(password) == 12
        assert all(c.isalnum() for c in password), "Should only contain alphanumeric characters"


class TestPhoneNumberGeneration:
    """Test phone number generation utility"""
    
    def test_generate_phone_number_default(self):
        """Test phone number generation with default country code"""
        phone = generate_phone_number()
        
        assert phone.startswith("+1-")
        assert len(phone) == 16  # +1-xxx-xxx-xxxx format
        
        # Extract and validate numbers
        parts = phone[3:].split("-")  # Remove +1- prefix
        assert len(parts) == 3
        
        area_code, exchange, number = parts
        assert len(area_code) == 3
        assert len(exchange) == 3
        assert len(number) == 4
        
        assert all(part.isdigit() for part in [area_code, exchange, number])
    
    def test_generate_phone_number_custom_country(self):
        """Test phone number generation with custom country code"""
        phone = generate_phone_number(country_code="+86")
        assert phone.startswith("+86-")


class TestVerificationCodeExtraction:
    """Test verification code extraction from text"""
    
    def test_extract_6_digit_code(self):
        """Test extraction of 6-digit verification code"""
        text = "Your verification code is: 123456"
        code = extract_verification_code(text)
        
        assert code == "123456"
    
    def test_extract_code_with_label(self):
        """Test extraction with various labels"""
        test_cases = [
            "Verification code: 789012",
            "验证码：789012",
            "Your code is 789012",
            "Code: 789012 Please enter",
        ]
        
        for text in test_cases:
            code = extract_verification_code(text)
            assert code == "789012", f"Failed to extract code from: {text}"
    
    def test_extract_4_digit_code(self):
        """Test extraction of 4-digit code when 6-digit not found"""
        text = "Your PIN is 1234"
        code = extract_verification_code(text)
        
        assert code == "1234"
    
    def test_extract_no_code(self):
        """Test when no code is found"""
        text = "This text has no verification code"
        code = extract_verification_code(text)
        
        assert code is None
    
    def test_extract_code_from_html(self):
        """Test extraction from HTML content"""
        html = "<p>Your verification code is: <strong>999888</strong></p>"
        code = extract_verification_code(html)
        
        assert code == "999888"


class TestEmailContentParsing:
    """Test HTML email content parsing"""
    
    def test_parse_simple_html(self):
        """Test parsing simple HTML to text"""
        html = "<h1>Title</h1><p>This is a paragraph.</p>"
        text = parse_email_content(html)
        
        assert "Title" in text
        assert "This is a paragraph." in text
    
    def test_parse_html_with_script_and_style(self):
        """Test that scripts and styles are removed"""
        html = """
        <script>alert('test');</script>
        <style>body { color: red; }</style>
        <p>Visible content here</p>
        """
        text = parse_email_content(html)
        
        assert "alert('test')" not in text
        assert "color: red" not in text
        assert "Visible content here" in text
    
    def test_parse_empty_content(self):
        """Test parsing empty content"""
        assert parse_email_content("") == ""
        assert parse_email_content(None) == ""


class TestEmailValidation:
    """Test email address validation"""
    
    @pytest.mark.parametrize("email,expected", [
        ("valid@example.com", True),
        ("test.email@domain.co.uk", True),
        ("user123@mail.org", True),
        ("invalid.email", False),
        ("@missing.domain.com", False),
        ("missing.domain.com", False),
        ("user@", False),
        ("", False),
        (None, False),
    ])
    def test_validate_email_address(self, email, expected):
        """Test email validation with various examples"""
        if email is None:
            # Handle None case
            with pytest.raises(TypeError):
                validate_email_address(email)
        else:
            result = validate_email_address(email)
            assert result == expected


class TestListChunking:
    """Test list chunking utility"""
    
    def test_chunk_list_equal_sizes(self):
        """Test chunking list into equal-sized chunks"""
        data = [1, 2, 3, 4, 5, 6, 7, 8]
        chunks = chunk_list(data, chunk_size=3)
        
        assert len(chunks) == 3
        assert chunks[0] == [1, 2, 3]
        assert chunks[1] == [4, 5, 6]
        assert chunks[2] == [7, 8]  # Last chunk can be smaller
    
    def test_chunk_list_single_item(self):
        """Test chunking into single-item chunks"""
        data = [1, 2, 3]
        chunks = chunk_list(data, chunk_size=1)
        
        assert len(chunks) == 3
        assert all(len(chunk) == 1 for chunk in chunks)
        assert chunks == [[1], [2], [3]]
    
    def test_chunk_list_empty_list(self):
        """Test chunking empty list"""
        chunks = chunk_list([], chunk_size=3)
        assert chunks == []
    
    def test_chunk_list_chunk_size_larger_than_list(self):
        """Test chunking with chunk size larger than list"""
        data = [1, 2]
        chunks = chunk_list(data, chunk_size=5)
        
        assert len(chunks) == 1
        assert chunks[0] == [1, 2]