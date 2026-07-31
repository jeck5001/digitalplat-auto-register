"""
Enhanced CLI for DigitalPlat Auto Register

This module provides an enhanced CLI interface with:
- Progress bars for long operations
- Interactive configuration wizard
- Colored table output
- Real-time status display
- Batch operations with progress tracking
"""

import asyncio
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import click
from loguru import logger
from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TaskProgressColumn,
)
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm, IntPrompt
from rich.layout import Layout
from rich.live import Live
from rich.syntax import Syntax
from rich.text import Text
from rich import box

from .core.account_pool import AccountPool, AccountStatus, SelectionStrategy
from .core.statistics import StatisticsCollector, MetricType
from .types import DigitalPlatConfig, UserProfile
from .utils.enhanced_logging import setup_enhanced_logging, get_logger, log_timing, log_aggregator


console = Console()
log = get_logger("cli_enhanced")


def print_banner():
    """Print ASCII art banner"""
    banner = r"""
    ╔═══════════════════════════════════════════════════════════════════╗
    ║   ____  _       _     _       _       _        ____       _     ║
    ║  |  _ \(_) __ _| (_)___| | __ _| |      / ___| __ _ | |_     ║
    ║  | | | | |/ _` | | / __| |/ _` | |     | |  _ / _` || __|    ║
    ║  | |_| | | (_| | | \__ \ | (_| | |___  | |_| || (_| || |_     ║
    ║  |____/|_|\__,_|_|_|___/_|\__,_|_____|  \____|\__,___| \__|   ║
    ║                                                               ║
    ║   ____   _   _   ____   _____   ____                          ║
    ║  |  _ \ | | | | |  _ \ | ____| |  _ \                         ║
    ║  | |_) || | | | | |_) ||  _|   | |_) |                        ║
    ║  |  __/ | |_| | |  _ < | |___  |  _ <                         ║
    ║  |_|     \___/  |_| \_\|_____| |_| \_\                        ║
    ╚═══════════════════════════════════════════════════════════════════╝
    """
    console.print(Panel(banner, style="bold cyan", border_style="cyan"))
    console.print()


def create_progress_bar(description: str) -> Progress:
    """Create a rich progress bar"""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        console=console,
    )


def display_table(title: str, columns: List[str], rows: List[List[Any]], 
                  header_style: str = "bold cyan"):
    """Display data as a rich formatted table"""
    table = Table(title=title, header_style=header_style, box=box.ROUNDED)
    for col in columns:
        table.add_column(col)
    for row in rows:
        table.add_row(*[str(cell) for cell in row])
    console.print(table)


def display_account_table(accounts: list, title: str = "Accounts"):
    """Display account information in a table"""
    columns = ["ID", "Username", "Email", "Status", "Uses", "Success Rate"]
    rows = []
    for acc in accounts:
        rows.append([
            acc.id[:12] + "...",
            acc.profile.get("username", ""),
            acc.profile.get("email", ""),
            _colorize_status(acc.status),
            str(acc.metrics.total_uses),
            f"{acc.metrics.success_rate:.1%}",
        ])
    display_table(title, columns, rows)


def display_stats_summary(stats: dict):
    """Display statistics summary panels"""
    # Create main grid
    grid = Table.grid(expand=True)
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    
    # Success/Failure row
    grid.add_row(
        Panel(
            Text(f"{stats.get('total_registrations', 0)}", justify="center", style="bold"),
            title="[blue]Total[/blue]",
            border_style="blue",
        ),
        Panel(
            Text(f"{stats.get('successful_registrations', 0)}", justify="center", style="bold green"),
            title="[green]Success[/green]",
            border_style="green",
        ),
        Panel(
            Text(f"{stats.get('failed_registrations', 0)}", justify="center", style="bold red"),
            title="[red]Failed[/red]",
            border_style="red",
        ),
        Panel(
            Text(f"{stats.get('registration_success_rate', 0):.1%}", justify="center", style="bold"),
            title="[cyan]Rate[/cyan]",
            border_style="cyan",
        ),
    )
    console.print(grid)
    
    # Additional stats
    table = Table(title="Additional Statistics", box=box.SIMPLE)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    
    table.add_row("Domains Registered", str(stats.get("total_domains_registered", 0)))
    table.add_row("Emails Created", str(stats.get("total_emails_created", 0)))
    table.add_row("Turnstile Solved", str(stats.get("total_turnstile_solved", 0)))
    table.add_row("Errors", str(stats.get("total_errors", 0)))
    table.add_row("Avg Duration", f"{stats.get('avg_registration_duration', 0):.2f}s")
    console.print(table)


def _colorize_status(status: str) -> str:
    """Add color to status string"""
    colors = {
        "active": "green",
        "in_use": "yellow",
        "cooling_down": "blue",
        "suspended": "red",
        "banned": "red bold",
        "password_expired": "yellow",
    }
    color = colors.get(status, "white")
    return f"[{color}]{status}[/{color}]"


def interactive_config_wizard() -> Dict[str, Any]:
    """Interactive configuration wizard"""
    console.print(Panel("Configuration Wizard", style="bold cyan"))
    console.print()
    
    config = {}
    
    # Base URL
    config["base_url"] = Prompt.ask(
        "Base URL",
        default="https://dash.domain.digitalplat.org",
    )
    
    # Registration endpoint
    config["registration_endpoint"] = Prompt.ask(
        "Registration endpoint",
        default="/auth/register",
    )
    
    # Turnstile settings
    console.print("\n[bold cyan]Cloudflare Turnstile Settings[/bold cyan]")
    config["turnstile"] = {}
    config["turnstile"]["enabled"] = Confirm.ask("Enable Turnstile solving?", default=True)
    
    solver_type = Prompt.ask(
        "Solver type",
        choices=["local", "remote", "mock"],
        default="remote",
    )
    config["turnstile"]["solver_type"] = solver_type
    
    if solver_type == "remote":
        config["turnstile"]["remote_endpoint"] = Prompt.ask(
            "Remote solver endpoint",
            default="http://192.168.5.35:5072",
        )
    
    # Email settings
    console.print("\n[bold cyan]Email Settings[/bold cyan]")
    config["email"] = {}
    config["email"]["provider"] = Prompt.ask(
        "Email provider",
        choices=["mail.td", "10minutemail", "guerrillamail"],
        default="mail.td",
    )
    
    # Browser settings
    console.print("\n[bold cyan]Browser Settings[/bold cyan]")
    config["browser"] = {}
    config["browser"]["headless"] = Confirm.ask("Run in headless mode?", default=True)
    config["browser"]["timeout"] = IntPrompt.ask(
        "Timeout (seconds)",
        default=30,
    ) * 1000  # Convert to ms
    
    # Operation settings
    console.print("\n[bold cyan]Operation Settings[/bold cyan]")
    config["max_registration_attempts"] = IntPrompt.ask(
        "Max registration attempts",
        default=3,
    )
    config["retry_delay"] = float(Prompt.ask(
        "Retry delay (seconds)",
        default="5.0",
    ))
    
    console.print("\n[green]Configuration complete![/green]")
    return config


def interactive_account_wizard() -> Dict[str, str]:
    """Interactive account creation wizard"""
    console.print(Panel("Account Creation", style="bold cyan"))
    console.print()
    
    account = {}
    account["username"] = Prompt.ask("Username (or leave blank for random)")
    account["email"] = Prompt.ask("Email")
    account["password"] = Prompt.ask("Password (or leave blank for random)")
    account["fullname"] = Prompt.ask("Full name (or leave blank for random)")
    account["phone"] = Prompt.ask("Phone (or leave blank for random)")
    
    return {k: v for k, v in account.items() if v}


# Enhanced CLI commands
@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose output")
@click.option("--log-file", type=str, default=None, help="Log file path")
@click.option("--structured-logs", is_flag=True, help="Enable JSON structured logging")
@click.pass_context
def cli(ctx, verbose, log_file, structured_logs):
    """DigitalPlat Auto Register - Enhanced CLI"""
    ctx.ensure_object(dict)
    
    log_level = "DEBUG" if verbose else "INFO"
    setup_enhanced_logging(
        level=log_level,
        log_file=log_file,
        structured=structured_logs,
    )
    
    ctx.obj["log_level"] = log_level
    
    if verbose:
        print_banner()


@cli.command()
def banner():
    """Display the application banner"""
    print_banner()


@cli.command()
@click.option("--config", "-c", type=str, help="Save configuration to file")
def wizard(config):
    """Run interactive configuration wizard"""
    result = interactive_config_wizard()
    
    if config:
        import yaml
        with open(config, "w") as f:
            yaml.dump(result, f, default_flow_style=False)
        console.print(f"[green]Configuration saved to {config}[/green]")
    else:
        console.print("\n[bold]Generated Configuration:[/bold]")
        syntax = Syntax(yaml.dump(result, default_flow_style=False), "yaml")
        console.print(syntax)


@cli.command(name="pool")
@click.option("--db", type=str, default="account_pool.db", help="Database path")
def pool_status(db):
    """Show account pool status"""
    pool = AccountPool(db_path=db)
    stats = pool.get_pool_stats()
    
    display_stats_summary({
        "total_registrations": stats.get("total_accounts", 0),
        "successful_registrations": stats.get("active_accounts", 0),
        "failed_registrations": stats.get("total_accounts", 0) - stats.get("active_accounts", 0),
        "registration_success_rate": stats.get("pool_health", 0),
        "total_domains_registered": stats.get("available_accounts", 0),
    })


@cli.command(name="pool-add")
@click.option("--db", type=str, default="account_pool.db", help="Database path")
@click.option("--username", "-u", type=str, help="Username")
@click.option("--email", "-e", type=str, help="Email")
@click.option("--password", "-p", type=str, help="Password")
@click.option("--tags", "-t", type=str, help="Comma-separated tags")
def pool_add(db, username, email, password, tags):
    """Add account to pool (interactive if no options given)"""
    pool = AccountPool(db_path=db)
    
    if not username or not email:
        data = interactive_account_wizard()
        username = username or data.get("username")
        email = email or data.get("email")
        password = password or data.get("password")
    
    from .utils.helpers import generate_random_username, generate_password, generate_phone_number
    
    profile = UserProfile(
        username=username or generate_random_username(),
        email=email,
        fullname=data.get("fullname", email.split("@")[0]) if not username else username,
        phone=data.get("phone", generate_phone_number()) if not username else generate_phone_number(),
        password=password or generate_password(),
    )
    
    entry = pool.add_account(
        profile,
        tags=[t.strip() for t in tags.split(",")] if tags else None,
    )
    console.print(f"[green]Account added: {entry.id}[/green]")


@cli.command(name="pool-list")
@click.option("--db", type=str, default="account_pool.db", help="Database path")
@click.option("--status", type=click.Choice([s.value for s in AccountStatus]), 
              default=None, help="Filter by status")
@click.option("--limit", "-l", type=int, default=50, help="Maximum number of accounts")
def pool_list(db, status, limit):
    """List accounts in pool"""
    pool = AccountPool(db_path=db)
    
    status_enum = AccountStatus(status) if status else None
    accounts = pool.list_all_accounts(status=status_enum, limit=limit)
    
    display_account_table(accounts, title=f"Account Pool ({len(accounts)} accounts)")


@cli.command(name="pool-health")
@click.option("--db", type=str, default="account_pool.db", help="Database path")
def pool_health(db):
    """Check account pool health"""
    pool = AccountPool(db_path=db)
    health = pool.health_check()
    
    columns = ["Account ID", "Healthy", "Issues", "Success Rate"]
    rows = []
    for acc_id, info in health.items():
        rows.append([
            acc_id[:12] + "...",
            "✅" if info["healthy"] else "❌",
            ", ".join(info["issues"]) if info["issues"] else "None",
            f"{info['success_rate']:.1%}",
        ])
    
    display_table("Health Check", columns, rows)


@cli.command(name="stats")
@click.option("--db", type=str, default="statistics.db", help="Database path")
@click.option("--days", "-d", type=int, default=7, help="Number of days to include")
def stats_dashboard(db, days):
    """Display statistics dashboard"""
    stats = StatisticsCollector(db_path=db)
    summary = stats.get_summary(days=days)
    
    display_stats_summary(summary.__dict__)
    
    # Recent activities
    if summary.recent_activities:
        columns = ["Username", "Email", "Success", "Time"]
        rows = [
            [
                act["username"],
                act["email"],
                "✅" if act["success"] else "❌",
                act["timestamp"][:19],
            ]
            for act in summary.recent_activities[:10]
        ]
        display_table("Recent Activities", columns, rows)
    
    # Error summary
    error_summary = log_aggregator.get_error_summary()
    if error_summary["total_errors"] > 0:
        console.print(f"\n[red]Total Errors: {error_summary['total_errors']}[/red]")
        for source, count in list(error_summary["error_by_source"].items())[:5]:
            console.print(f"  - {source}: {count}")


@cli.command(name="logs")
@click.option("--level", type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]),
              default=None, help="Filter by log level")
@click.option("--limit", "-l", type=int, default=20, help="Maximum number of logs")
@click.option("--minutes", "-m", type=int, default=None, help="Show logs from last N minutes")
def show_logs(level, limit, minutes):
    """Display recent log events"""
    events = log_aggregator.get_recent_events(
        limit=limit,
        level=level,
        since_minutes=minutes,
    )
    
    columns = ["Time", "Level", "Message", "Context"]
    rows = []
    for event in events:
        context_str = ", ".join(
            f"{k}={v}" for k, v in event.get("context", {}).items()
        )
        rows.append([
            event["timestamp"][:19],
            _colorize_level(event["level"]),
            event["message"][:50],
            context_str[:30],
        ])
    
    display_table("Recent Logs", columns, rows)


def _colorize_level(level: str) -> str:
    """Add color to log level"""
    colors = {
        "TRACE": "dim",
        "DEBUG": "blue",
        "INFO": "white",
        "SUCCESS": "green",
        "WARNING": "yellow",
        "ERROR": "red",
        "CRITICAL": "red bold",
    }
    color = colors.get(level, "white")
    return f"[{color}]{level}[/{color}]"


@cli.command(name="batch-register")
@click.option("--count", "-n", type=int, default=1, help="Number of accounts to register")
@click.option("--config", "-c", type=str, help="Configuration file path")
@click.option("--db", type=str, default="account_pool.db", help="Account pool database")
@click.option("--stats-db", type=str, default="statistics.db", help="Statistics database")
@click.option("--delay", type=float, default=10.0, help="Delay between registrations")
@click.option("--strategy", 
              type=click.Choice([s.value for s in SelectionStrategy]),
              default="least_recently_used",
              help="Account selection strategy")
def batch_register(count, config, db, stats_db, delay, strategy):
    """Register multiple accounts with progress tracking"""
    stats_collector = StatisticsCollector(db_path=stats_db)
    pool = AccountPool(db_path=db)
    
    print_banner()
    console.print(f"[bold]Batch Registration[/bold] - {count} accounts")
    console.print(f"Strategy: {strategy} | Delay: {delay}s\n")
    
    success_count = 0
    failure_count = 0
    
    with create_progress_bar("Registering") as progress:
        task = progress.add_task("Starting...", total=count)
        
        for i in range(count):
            progress.update(task, description=f"Registering {i+1}/{count}...")
            
            start_time = time.time()
            
            try:
                # Simulate registration (replace with actual registration logic)
                time.sleep(0.5)  # Placeholder
                
                stats_collector.record_metric(MetricType.REGISTRATION_SUCCESS)
                success_count += 1
                progress.update(task, advance=1)
                
            except Exception as e:
                stats_collector.record_metric(MetricType.REGISTRATION_FAILURE)
                failure_count += 1
                log.error(f"Registration {i+1} failed: {e}")
                progress.update(task, advance=1)
            
            # Delay between registrations
            if i < count - 1:
                time.sleep(delay)
    
    # Display results
    console.print(f"\n[green]✅ Successful: {success_count}[/green]")
    if failure_count > 0:
        console.print(f"[red]❌ Failed: {failure_count}[/red]")
    console.print(f"Success rate: {success_count/(success_count+failure_count):.1%}")


@cli.command(name="interactive")
def interactive_mode():
    """Launch interactive shell mode"""
    print_banner()
    
    console.print("[bold cyan]Interactive Mode[/bold cyan]")
    console.print("Type 'help' for available commands, 'quit' to exit\n")
    
    pool = AccountPool()
    stats = StatisticsCollector()
    
    while True:
        try:
            command = Prompt.ask("[bold green]>>>[/bold green]")
            
            if command.lower() in ("quit", "exit", "q"):
                console.print("[yellow]Goodbye![/yellow]")
                break
            
            elif command == "help":
                display_table(
                    "Available Commands",
                    ["Command", "Description"],
                    [
                        ["status", "Show pool and stats summary"],
                        ["list", "List active accounts"],
                        ["health", "Check account health"],
                        ["logs", "Show recent logs"],
                        ["clean", "Clean up old data"],
                        ["help", "Show this help"],
                        ["quit", "Exit interactive mode"],
                    ],
                )
            
            elif command == "status":
                pool_stats = pool.get_pool_stats()
                stat_summary = stats.get_summary(days=7)
                display_stats_summary({
                    **stat_summary.__dict__,
                    "pool_total": pool_stats.get("total_accounts", 0),
                })
            
            elif command == "list":
                accounts = pool.list_available_accounts()
                display_account_table(accounts, title="Available Accounts")
            
            elif command == "health":
                health = pool.health_check()
                for acc_id, info in list(health.items())[:5]:
                    status = "✅" if info["healthy"] else "❌"
                    console.print(f"{status} {acc_id[:12]}...")
            
            elif command == "logs":
                events = log_aggregator.get_recent_events(limit=10)
                for event in events:
                    console.print(f"[{event['level']}] {event['message'][:50]}")
            
            elif command == "clean":
                deleted = stats.cleanup_old_data()
                console.print(f"Cleaned {deleted} old records")
            
            else:
                console.print(f"[red]Unknown command: {command}[/red]")
        
        except KeyboardInterrupt:
            console.print("\n[yellow]Goodbye![/yellow]")
            break
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")


# Entry point
if __name__ == "__main__":
    cli()