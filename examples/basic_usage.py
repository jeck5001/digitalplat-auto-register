#!/usr/bin/env python3
"""
Basic usage examples for DigitalPlat Auto Register

This file demonstrates various ways to use the DigitalPlat Auto Register package.
"""

import asyncio
import json
from pathlib import Path

from digitalplat_auto_register import DigitalPlatRegistrar
from digitalplat_auto_register.types import DigitalPlatConfig
from digitalplat_auto_register.core.registrar import register_with_defaults
from digitalplat_auto_register.core.result import StepResult


async def example_1_basic_registration():
    """Example 1: Basic registration with automatic data generation"""
    print("=" * 60)
    print("Example 1: Basic Registration")
    print("=" * 60)
    
    # Use default configuration
    result = await register_with_defaults(
        referral_code="abc123"
    )
    
    if result.success:
        print(f"✅ Registration successful!")
        print(f"   Username: {result.username}")
        print(f"   Email: {result.email}")
        print(f"   Duration: {result.total_duration:.2f}s")
    else:
        print(f"❌ Registration failed: {result.error}")
    
    print()


async def example_2_custom_user_data():
    """Example 2: Registration with custom user data"""
    print("=" * 60)
    print("Example 2: Custom User Data")
    print("=" * 60)
    
    result = await register_with_defaults(
        username="mycustomuser",
        fullname="John Doe",
        phone="+1-555-123-4567",
        password="MySecurePass123!",
        referral_code="xyz789"
    )
    
    if result.success:
        print(f"✅ Custom registration successful!")
        print(f"   Username: {result.username}")
        print(f"   Email: {result.email}")
        print(f"   Steps completed: {result.steps_successful}/{result.steps_completed}")
    else:
        print(f"❌ Custom registration failed: {result.error}")
    
    print()


async def example_3_progress_monitoring():
    """Example 3: Registration with progress monitoring"""
    print("=" * 60)
    print("Example 3: Progress Monitoring")
    print("=" * 60)
    
    def progress_callback(step_result: StepResult):
        status_icon = "✅" if step_result.success else "❌" if step_result.error else "⏳"
        print(f"  {status_icon} {step_result.name}: {step_result.message}")
    
    result = await register_with_defaults(
        referral_code="prog123",
        on_step_complete=progress_callback
    )
    
    if result.success:
        print(f"✅ Registration completed with monitoring!")
        print(f"   Username: {result.username}")
    else:
        print(f"❌ Monitored registration failed: {result.error}")
    
    print()


async def example_4_custom_configuration():
    """Example 4: Registration with custom configuration"""
    print("=" * 60)
    print("Example 4: Custom Configuration")
    print("=" * 60)
    
    # Create custom configuration
    config = DigitalPlatConfig(
        base_url="https://dash.domain.digitalplat.org",
        turnstile={
            "solver_type": "mock",  # Use mock for example
            "timeout": 30,
            "max_retries": 2
        },
        email={
            "provider": "mail.td",
            "timeout": 120
        },
        browser={
            "headless": True,
            "timeout": 15000
        },
        max_registration_attempts=2,
        verification_timeout=60
    )
    
    registrar = DigitalPlatRegistrar(config)
    result = await registrar.register_account(
        username="configuser",
        fullname="Config User",
        referral_code="config123"
    )
    
    if result.success:
        print(f"✅ Custom config registration successful!")
        print(f"   Username: {result.username}")
        print(f"   Turnstile result: {result.turnstile_result}")
        print(f"   Email result: {result.email_result}")
    else:
        print(f"❌ Custom config registration failed: {result.error}")
    
    print()


async def example_5_result_serialization():
    """Example 5: Saving registration results to JSON"""
    print("=" * 60)
    print("Example 5: Result Serialization")
    print("=" * 60)
    
    result = await register_with_defaults(
        username="serialuser",
        referral_code="serial123"
    )
    
    # Save result to JSON file
    output_file = Path("registration_result.json")
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(result.to_dict(), f, indent=2, ensure_ascii=False)
    
    print(f"✅ Result saved to: {output_file}")
    
    # Display summary
    summary = json.loads(result.to_json())
    print(f"   Status: {summary['success']}")
    print(f"   Username: {summary['username']}")
    print(f"   Email: {summary['email']}")
    print(f"   Duration: {summary['total_duration']}s")
    
    print()


async def example_6_error_handling():
    """Example 6: Demonstrating error handling"""
    print("=" * 60)
    print("Example 6: Error Handling")
    print("=" * 60)
    
    # Create configuration that will likely fail
    bad_config = DigitalPlatConfig(
        base_url="https://invalid-domain.example.com",  # Invalid URL
        turnstile={
            "solver_type": "remote",
            "remote_endpoint": "http://invalid-solver:9999"  # Invalid endpoint
        },
        max_registration_attempts=1  # Don't retry
    )
    
    registrar = DigitalPlatRegistrar(bad_config)
    result = await registrar.register_account(
        username="erroruser",
        referral_code="error123"
    )
    
    print(f"❌ Expected failure occurred:")
    print(f"   Error: {result.error}")
    print(f"   Failed stage: {result.error_stage}")
    print(f"   Retry attempts: {result.retry_attempts}")
    
    if result.step_results:
        print(f"   Failed steps:")
        for step in result.get_failed_steps():
            print(f"     - {step.name}: {step.error or 'Unknown error'}")
    
    print()


async def example_7_batch_registration_simulation():
    """Example 7: Simulate batch registration"""
    print("=" * 60)
    print("Example 7: Batch Registration Simulation")
    print("=" * 60)
    
    # Simulate batch of users
    users = [
        {"username": f"batchuser_{i}", "fullname": f"Batch User {i}"}
        for i in range(3)
    ]
    
    results = []
    for user_data in users:
        print(f"Processing {user_data['username']}...")
        
        result = await register_with_defaults(
            **user_data,
            referral_code="batch123"
        )
        
        results.append(result)
        await asyncio.sleep(1)  # Small delay between registrations
    
    # Summary
    successful = sum(1 for r in results if r.success)
    total = len(results)
    
    print(f"✅ Batch registration complete:")
    print(f"   Successful: {successful}/{total}")
    print(f"   Success rate: {successful/total*100:.1f}%")
    
    for result in results:
        status = "✅" if result.success else "❌"
        print(f"   {status} {result.username or 'Unknown'}: "
              f"{result.error or 'Success'}")
    
    print()


async def main():
    """Run all examples"""
    print("DigitalPlat Auto Register - Usage Examples")
    print("=" * 60)
    print()
    
    print("Note: Some examples use mock services for demonstration.")
    print("In real usage, configure proper Turnstile solver and email provider.")
    print()
    
    try:
        # Run examples
        await example_1_basic_registration()
        await example_2_custom_user_data()
        await example_3_progress_monitoring()
        await example_4_custom_configuration()
        await example_5_result_serialization()
        await example_6_error_handling()
        await example_7_batch_registration_simulation()
        
        print("✅ All examples completed!")
        
    except KeyboardInterrupt:
        print("\n⚠️  Examples interrupted by user")
    except Exception as e:
        print(f"\n❌ Error running examples: {str(e)}")


if __name__ == "__main__":
    asyncio.run(main())