"""
Command-line interface for DigitalPlat auto registration

This module provides a CLI interface to perform DigitalPlat registrations
from the command line with various options and configurations.
"""

import asyncio
import sys
import signal
from pathlib import Path
from typing import Optional, List
import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.panel import Panel
from rich.traceback import install
from loguru import logger

from .core.config import ConfigManager
from .core.registrar import DigitalPlatRegistrar, register_with_defaults
from .core.result import RegistrationResult, StepResult
from .types import DigitalPlatConfig
from .exceptions import DigitalPlatError


# Install rich traceback for better error reporting
install(show_locals=True)

# Global console for rich output
console = Console()


@click.group()
@click.version_option(package_name="digitalplat-auto-register")
@click.option('--verbose', '-v', is_flag=True, help="Enable verbose logging")
@click.option('--quiet', '-q', is_flag=True, help="Suppress most output")
@click.option('--log-file', type=click.Path(), help="Log file path")
@click.pass_context
def cli(ctx, verbose: bool, quiet: bool, log_file: Optional[str]):
    """DigitalPlat Auto Registration Tool
    
    Automate DigitalPlat domain registration using temporary email
    and Cloudflare Turnstile bypass techniques.
    
    Examples:
        \b
        digitalplat-register single --referral-code=abc123
        digitalplat-register batch registrations.json
        digitalplat-register config generate
    """
    
    # Configure logging based on options
    log_level = "DEBUG" if verbose else ("ERROR" if quiet else "INFO")
    
    # Remove default logger
    logger.remove()
    
    # Add console handler
    if not quiet:
        logger.add(
            sys.stderr,
            level=log_level,
            format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                  "<level>{level: <8}</level> | "
                  "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
                  "<level>{message}</level>",
            colorize=True
        )
    
    # Add file handler if specified
    if log_file:
        logger.add(
            log_file,
            level="DEBUG",
            rotation="10 MB",
            retention="1 week",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}"
        )
    
    # Store context
    ctx.ensure_object(dict)
    ctx.obj['verbose'] = verbose
    ctx.obj['quiet'] = quiet
    ctx.obj['log_file'] = log_file


@cli.command()
@click.option('--username', '-u', help='Username for the account')
@click.option('--email', '-e', help='Email address (will be auto-generated if not provided)')
@click.option('--password', '-p', help='Password (will be auto-generated if not provided)')
@click.option('--fullname', '-n', help='Full name')
@click.option('--phone', help='Phone number')
@click.option('--address-line1', help='WHOIS address line 1')
@click.option('--address-line2', help='WHOIS address line 2')
@click.option('--city', help='WHOIS city')
@click.option('--state', help='WHOIS state or province')
@click.option('--postal-code', help='WHOIS postal code')
@click.option('--country', help='WHOIS ISO 3166-1 alpha-2 country code')
@click.option('--referral-code', '-r', help='Referral code to use')
@click.option('--config', '-c', type=click.Path(exists=True), help='Configuration file')
@click.option('--output', '-o', type=click.Path(), help='Output result to JSON file')
@click.option('--verbose-steps', is_flag=True, help='Show detailed step progress')
@click.pass_context
def single(
    ctx, 
    username: Optional[str],
    email: Optional[str], 
    password: Optional[str],
    fullname: Optional[str],
    phone: Optional[str],
    address_line1: Optional[str],
    address_line2: Optional[str],
    city: Optional[str],
    state: Optional[str],
    postal_code: Optional[str],
    country: Optional[str],
    referral_code: Optional[str],
    config: Optional[str],
    output: Optional[str],
    verbose_steps: bool
):
    """Register a single DigitalPlat account"""
    
    console.print(Panel.fit(
        "[bold blue]DigitalPlat Single Registration[/]",
        subtitle="Starting registration process..."
    ))
    
    try:
        # Show configuration if verbose
        if ctx.obj['verbose'] and config:
            console.print(f"[dim]Using config file: {config}[/]")
        
        # Progress callback for step tracking
        def step_callback(step_result: StepResult):
            if verbose_steps:
                status_icon = "✅" if step_result.success else "❌" if step_result.error else "⏳"
                console.print(f"  {status_icon} {step_result.name}: {step_result.message}")
        
        # Run registration in async context
        async def run_registration():
            return await register_with_defaults(
                username=username,
                email=email,
                password=password, 
                fullname=fullname,
                phone=phone,
                address_line1=address_line1,
                address_line2=address_line2,
                city=city,
                state=state,
                postal_code=postal_code,
                country=country,
                referral_code=referral_code or "",
                config_file=config,
                on_step_complete=step_callback if verbose_steps else None
            )
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=console,
            transient=True
        ) as progress:
            task = progress.add_task("Registering account...", total=None)
            result = asyncio.run(run_registration())
            progress.update(task, completed=True)
        
        # Display results
        _display_registration_result(result)
        
        # Save to output file if specified
        if output:
            import json
            with open(output, 'w', encoding='utf-8') as f:
                json.dump(result.to_dict(), f, indent=2, ensure_ascii=False)
            console.print(f"[green]Results saved to: {output}[/]")
        
        # Exit with appropriate code
        sys.exit(0 if result.success else 1)
        
    except KeyboardInterrupt:
        console.print("\n[yellow]Registration cancelled by user[/]")
        sys.exit(130)
        
    except Exception as e:
        console.print(f"[red]Error: {str(e)}[/]")
        if ctx.obj['verbose']:
            console.print_exception()
        sys.exit(1)


@cli.command()
@click.argument('batch_file', type=click.Path(exists=True))
@click.option('--config', '-c', type=click.Path(exists=True), help='Configuration file')
@click.option('--output-dir', '-o', type=click.Path(), help='Output directory for results')
@click.option('--delay', default=5.0, help='Delay between registrations (seconds)')
@click.option('--max-concurrent', default=1, help='Maximum concurrent registrations')
@click.option('--verbose-steps', is_flag=True, help='Show detailed step progress for each registration')
@click.pass_context
def batch(
    ctx,
    batch_file: str,
    config: Optional[str],
    output_dir: Optional[str],
    delay: float,
    max_concurrent: int,
    verbose_steps: bool
):
    """Register multiple DigitalPlat accounts from a batch file"""
    
    console.print(Panel.fit(
        "[bold blue]DigitalPlat Batch Registration[/]",
        subtitle="Processing multiple registrations..."
    ))
    
    # Load batch file
    try:
        import json
        with open(batch_file, 'r', encoding='utf-8') as f:
            batch_data = json.load(f)
        
        if not isinstance(batch_data, list):
            raise ValueError("Batch file must contain a JSON array of registration data")
        
        registrations = batch_data
        console.print(f"[green]Loaded {len(registrations)} registrations from batch file[/]")
        
    except Exception as e:
        console.print(f"[red]Error loading batch file: {str(e)}[/]")
        sys.exit(1)
    
    # Create output directory if needed
    if output_dir:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
    
    # Run batch registrations
    async def run_batch():
        results = []
        semaphore = asyncio.Semaphore(max_concurrent)
        
        async def register_single(registration_data, index):
            async with semaphore:
                try:
                    # Environment values are the baseline for containers;
                    # an optional file retains higher precedence.
                    config_manager = ConfigManager().load_from_env()
                    if config:
                        config_manager.load_from_file(config)
                    app_config = config_manager.load()
                    
                    # Create registrar
                    registrar = DigitalPlatRegistrar(app_config)
                    
                    # Progress callback
                    def step_callback(step_result: StepResult):
                        if verbose_steps:
                            status_icon = "✅" if step_result.success else "❌" if step_result.error else "⏳"
                            console.print(f"  [dim]Registration {index}:[/] {status_icon} {step_result.name}")
                    
                    result = await registrar.register_account(
                        username=registration_data.get('username'),
                        email=registration_data.get('email'),
                        password=registration_data.get('password'),
                        fullname=registration_data.get('fullname'),
                        phone=registration_data.get('phone'),
                        referral_code=registration_data.get('referral_code', ''),
                        on_step_complete=step_callback if verbose_steps else None
                    )
                    
                    results.append(result)
                    return result
                    
                except Exception as e:
                    console.print(f"[red]Registration {index} failed: {str(e)}[/]")
                    return RegistrationResult(success=False, error=str(e))
                finally:
                    if delay > 0 and index < len(registrations) - 1:
                        await asyncio.sleep(delay)
        
        # Run all registrations concurrently (up to max_concurrent)
        tasks = [
            register_single(reg_data, i) 
            for i, reg_data in enumerate(registrations)
        ]
        
        await asyncio.gather(*tasks)
        return results
    
    try:
        with Progress(console=console) as progress:
            task = progress.add_task("Processing batch registrations...", total=len(registrations))
            
            results = asyncio.run(run_batch())
            
            # Update progress
            progress.update(task, completed=len(registrations))
        
        # Display summary
        _display_batch_results(results, output_dir)
        
        sys.exit(0)
        
    except KeyboardInterrupt:
        console.print("\n[yellow]Batch registration cancelled by user[/]")
        sys.exit(130)


@cli.command()
@click.option('--format', type=click.Choice(['json', 'yaml', 'toml']), default='yaml')
@click.option('--output', '-o', type=click.Path(), help='Output file (default: stdout)')
def config_generate(format: str, output: Optional[str]):
    """Generate a sample configuration file"""
    
    from .types import DigitalPlatConfig
    
    # Create sample config
    sample_config = DigitalPlatConfig()
    config_dict = sample_config.dict(exclude_none=True)
    
    try:
        if format == 'json':
            import json
            content = json.dumps(config_dict, indent=2, ensure_ascii=False)
        elif format == 'yaml':
            import yaml
            content = yaml.dump(config_dict, indent=2, allow_unicode=True)
        elif format == 'toml':
            try:
                import tomli_w
                content = tomli_w.dumps(config_dict)
            except ImportError:
                try:
                    import toml
                    content = toml.dumps(config_dict)
                except ImportError:
                    console.print("[red]TOML support requires 'tomli_w' or 'toml' package[/]")
                    sys.exit(1)
        
        # Write to file or stdout
        if output:
            with open(output, 'w', encoding='utf-8') as f:
                f.write(content)
            console.print(f"[green]Configuration written to: {output}[/]")
        else:
            console.print(content)
            
        console.print("\n[blue]Tip:[/] Edit the configuration file for your specific setup.")
        
    except Exception as e:
        console.print(f"[red]Error generating configuration: {str(e)}[/]")
        sys.exit(1)


@cli.command()
def version():
    """Show version information"""
    from . import __version__, __author__, __description__
    
    console.print(f"[bold]DigitalPlat Auto Register[/] v{__version__}")
    console.print(f"{__description__}")
    console.print(f"Author: {__author__}")


def _display_registration_result(result: RegistrationResult):
    """Display a single registration result in a formatted table"""
    
    # Create results table
    table = Table(title="Registration Results")
    table.add_column("Property", style="cyan")
    table.add_column("Value", style="green")
    
    table.add_row("Status", "✅ Success" if result.success else "❌ Failed")
    table.add_row("Registration ID", result.registration_id)
    
    if result.username:
        table.add_row("Username", result.username)
    
    if result.email:
        table.add_row("Email", result.email)
    
    table.add_row("Account Created", "✅" if result.account_created else "❌")
    table.add_row("Email Verified", "✅" if result.email_verified else "❌")
    
    if result.total_duration:
        table.add_row("Total Duration", f"{result.total_duration:.2f}s")
    
    if result.referral_code:
        table.add_row("Referral Code", result.referral_code)
    
    # Add error info if failed
    if not result.success and result.error:
        table.add_row("Error", f"[red]{result.error}[/]")
        if result.error_stage:
            table.add_row("Failed Stage", result.error_stage)
    
    # Add step summary
    if result.step_results:
        successful_steps = result.steps_successful
        total_steps = result.steps_completed
        table.add_row("Steps Completed", f"{successful_steps}/{total_steps}")
    
    console.print(table)
    
    # Show step details if available
    if result.step_results and len(result.step_results) > 0:
        step_table = Table(title="Step Details")
        step_table.add_column("Step")
        step_table.add_column("Status")
        step_table.add_column("Duration")
        
        for step in result.step_results:
            status = "✅" if step.success else "❌"
            duration = f"{step.duration:.2f}s" if step.duration else "- "
            step_table.add_row(step.name, status, duration)
        
        console.print(step_table)


def _display_batch_results(results: List[RegistrationResult], output_dir: Optional[str]):
    """Display batch registration results summary"""
    
    successful = sum(1 for r in results if r.success)
    failed = len(results) - successful
    
    console.print(Panel.fit(
        f"[bold]Batch Registration Complete[/]\n"
        f"✅ Successful: {successful}\n"
        f"❌ Failed: {failed}\n"
        f"📊 Success Rate: {successful/len(results)*100:.1f}%",
        style="green" if failed == 0 else "yellow"
    ))
    
    # Save individual results if output directory specified
    if output_dir:
        import json
        for i, result in enumerate(results):
            filename = f"registration_{i+1:03d}_{result.registration_id}.json"
            filepath = Path(output_dir) / filename
            
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(result.to_dict(), f, indent=2, ensure_ascii=False)
        
        console.print(f"[blue]Individual results saved to: {output_dir}[/]")
    
    # Show failures
    if failed > 0:
        console.print("\n[bold red]Failed Registrations:[/]")
        for result in results:
            if not result.success:
                console.print(f"  ❌ {result.username or 'Unknown'}: {result.error}")


def signal_handler(signum, frame):
    """Handle interruption signals gracefully"""
    console.print(f"\n[yellow]Received signal {signum}, shutting down...[/]")
    sys.exit(130)


# Set up signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def main():
    """Main entry point for CLI"""
    return cli()

if __name__ == '__main__':
    cli()
