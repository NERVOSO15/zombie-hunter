"""Main entry point for Zombie Hunter."""

import sys
import uuid
from pathlib import Path

import click
import structlog
from rich.console import Console
from rich.table import Table

from zombie_hunter import __version__
from zombie_hunter.config import Settings, init_settings, SlackMode
from zombie_hunter.resources.types import AggregatedScanResult, CloudProvider
from zombie_hunter.scanners.base import ScannerRegistry
from zombie_hunter.slack.notifier import SlackNotifier

# Configure structlog
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()
console = Console()


@click.group()
@click.version_option(version=__version__)
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=True, path_type=Path),
    help="Path to configuration file",
)
@click.option(
    "--dry-run/--no-dry-run",
    default=None,
    help="Enable/disable dry run mode",
)
@click.option(
    "--demo",
    is_flag=True,
    default=False,
    help="Use mock scanner with fake data (no cloud account needed)",
)
@click.pass_context
def cli(ctx: click.Context, config: Path | None, dry_run: bool | None, demo: bool) -> None:
    """
    Zombie Hunter - Find and eliminate zombie cloud resources.

    A FinOps tool that scans cloud providers for unused resources,
    estimates cost savings, and enables cleanup via Slack.
    
    Use --demo flag to test without cloud accounts.
    """
    ctx.ensure_object(dict)

    # Initialize settings
    settings = init_settings(config)

    # Override dry_run if specified
    if dry_run is not None:
        settings = settings.model_copy(update={"dry_run": dry_run})

    # Register mock scanner for demo mode
    if demo:
        from zombie_hunter.scanners.mock import register_mock_scanner
        register_mock_scanner()
        console.print("[cyan]🎭 Demo mode enabled - using mock data[/cyan]\n")

    ctx.obj["settings"] = settings
    ctx.obj["demo"] = demo


@cli.command()
@click.option(
    "--provider",
    "-p",
    type=click.Choice(["aws", "gcp", "azure", "all"]),
    default="all",
    help="Cloud provider to scan",
)
@click.option(
    "--region",
    "-r",
    multiple=True,
    help="Specific region(s) to scan (can be repeated)",
)
@click.option(
    "--notify/--no-notify",
    default=True,
    help="Send Slack notifications",
)
@click.option(
    "--output",
    "-o",
    type=click.Choice(["table", "json", "summary"]),
    default="table",
    help="Output format",
)
@click.pass_context
def scan(
    ctx: click.Context,
    provider: str,
    region: tuple[str, ...],
    notify: bool,
    output: str,
) -> None:
    """
    Scan for zombie resources across cloud providers.

    Examples:

        # Scan all configured providers
        zombie-hunter scan

        # Scan only AWS
        zombie-hunter scan --provider aws

        # Scan specific regions
        zombie-hunter scan --provider aws --region us-east-1 --region us-west-2

        # Scan without Slack notification
        zombie-hunter scan --no-notify

        # Output as JSON
        zombie-hunter scan --output json
    """
    settings: Settings = ctx.obj["settings"]

    # Override regions if specified
    if region:
        if provider == "aws" or provider == "all":
            settings.scanner.aws_regions = list(region)
        if provider == "gcp" or provider == "all":
            settings.scanner.gcp_regions = list(region)
        if provider == "azure" or provider == "all":
            settings.scanner.azure_regions = list(region)

    # Determine which providers to scan
    if provider == "all":
        providers_to_scan = settings.scanner.enabled_providers
    else:
        providers_to_scan = [CloudProvider(provider)]

    # Generate scan ID
    scan_id = str(uuid.uuid4())[:8]

    logger.info(
        "starting_scan",
        scan_id=scan_id,
        providers=[p.value for p in providers_to_scan],
        dry_run=settings.dry_run,
    )

    if settings.dry_run:
        console.print("[yellow]⚠️  DRY RUN MODE - No deletions will occur[/yellow]\n")

    # Perform scans
    aggregated_result = AggregatedScanResult(scan_id=scan_id)

    with console.status("[bold green]Scanning for zombies...") as status:
        for cloud_provider in providers_to_scan:
            status.update(f"[bold green]Scanning {cloud_provider.value.upper()}...")

            try:
                scanner = ScannerRegistry.get_scanner(cloud_provider, settings)
                result = scanner.scan_all()
                aggregated_result.results.append(result)

                console.print(
                    f"✓ {cloud_provider.value.upper()}: "
                    f"Found {result.zombie_count} zombies "
                    f"(${result.total_monthly_savings:.2f}/month)"
                )

            except ValueError as e:
                console.print(f"[red]✗ {cloud_provider.value.upper()}: {e}[/red]")
                logger.error("scanner_error", provider=cloud_provider.value, error=str(e))

    # Output results
    console.print()
    _output_results(aggregated_result, output)

    # Send Slack notification
    if notify and settings.slack.bot_token:
        console.print("\n[bold]Sending Slack notification...[/bold]")
        notifier = SlackNotifier(settings)
        if notifier.send_scan_results(aggregated_result):
            console.print("[green]✓ Slack notification sent[/green]")
        else:
            console.print("[red]✗ Failed to send Slack notification[/red]")

    logger.info(
        "scan_completed",
        scan_id=scan_id,
        total_zombies=aggregated_result.total_zombie_count,
        total_savings=aggregated_result.total_monthly_savings,
    )


@cli.command()
@click.pass_context
def serve(ctx: click.Context) -> None:
    """
    Start the Slack interactive handler server.

    This runs a server that listens for Slack button interactions
    to handle delete/ignore actions.

    Requires SLACK_APP_TOKEN environment variable for socket mode.
    """
    settings: Settings = ctx.obj["settings"]

    if settings.slack.mode != SlackMode.INTERACTIVE:
        console.print(
            "[yellow]Warning: Slack mode is not set to 'interactive'. "
            "Button interactions won't work.[/yellow]"
        )

    console.print("[bold]Starting Slack interactive handler...[/bold]")
    console.print("Press Ctrl+C to stop\n")

    from zombie_hunter.slack.interactive import create_slack_handler

    handler = create_slack_handler(settings)

    try:
        handler.start()
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down...[/yellow]")


@cli.command()
@click.argument("resource_id")
@click.option(
    "--provider",
    "-p",
    type=click.Choice(["aws", "gcp", "azure"]),
    required=True,
    help="Cloud provider",
)
@click.option(
    "--type",
    "-t",
    "resource_type",
    required=True,
    help="Resource type (e.g., ebs_volume, elastic_ip)",
)
@click.option(
    "--region",
    "-r",
    required=True,
    help="Resource region",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Skip confirmation prompt",
)
@click.pass_context
def delete(
    ctx: click.Context,
    resource_id: str,
    provider: str,
    resource_type: str,
    region: str,
    force: bool,
) -> None:
    """
    Delete a specific zombie resource.

    Example:

        zombie-hunter delete vol-0abc123 --provider aws --type ebs_volume --region us-east-1
    """
    from zombie_hunter.resources.types import ResourceType, ZombieReason, ZombieResource

    settings: Settings = ctx.obj["settings"]

    # Validate resource type
    try:
        rt = ResourceType(resource_type)
    except ValueError:
        valid_types = ", ".join(t.value for t in ResourceType)
        console.print(f"[red]Invalid resource type. Valid types: {valid_types}[/red]")
        sys.exit(1)

    # Confirmation
    if not force and not settings.dry_run:
        if not click.confirm(f"Are you sure you want to delete {resource_id}?"):
            console.print("[yellow]Aborted.[/yellow]")
            return

    # Create resource
    zombie = ZombieResource(
        id=resource_id,
        provider=CloudProvider(provider),
        resource_type=rt,
        region=region,
        reason=ZombieReason.UNUSED,
    )

    # Get scanner and delete
    try:
        scanner = ScannerRegistry.get_scanner(CloudProvider(provider), settings)
        success, message = scanner.safe_delete(zombie)

        if success:
            console.print(f"[green]✓ {message}[/green]")
        else:
            console.print(f"[red]✗ {message}[/red]")
            sys.exit(1)

    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


@cli.command()
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """Show current configuration."""
    settings: Settings = ctx.obj["settings"]

    console.print("[bold]Current Configuration:[/bold]\n")

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Setting", style="cyan")
    table.add_column("Value")

    # General settings
    table.add_row("Dry Run", str(settings.dry_run))
    table.add_row("Config Path", str(settings.config_path) if settings.config_path else "None")

    # Scanner settings
    table.add_row(
        "Enabled Providers",
        ", ".join(p.value for p in settings.scanner.enabled_providers),
    )
    table.add_row("AWS Regions", ", ".join(settings.scanner.aws_regions))
    table.add_row("GCP Regions", ", ".join(settings.scanner.gcp_regions))
    table.add_row("Azure Regions", ", ".join(settings.scanner.azure_regions))

    # Thresholds
    table.add_row("Snapshot Age Threshold", f"{settings.thresholds.snapshot_age_days} days")
    table.add_row("LB Idle Threshold", f"{settings.thresholds.lb_idle_days} days")
    table.add_row("Min Cost Threshold", f"${settings.thresholds.min_cost_threshold:.2f}")

    # Slack
    table.add_row("Slack Mode", settings.slack.mode.value)
    table.add_row("Slack Channel", settings.slack.channel)
    table.add_row(
        "Slack Bot Token",
        "****" + settings.slack.bot_token[-4:] if settings.slack.bot_token else "Not set",
    )

    # Logging
    table.add_row("Log Level", settings.logging.level)
    table.add_row("Log Format", settings.logging.format)

    console.print(table)


def _output_results(results: AggregatedScanResult, format: str) -> None:
    """Output scan results in the specified format."""
    if format == "json":
        import json

        output = {
            "scan_id": results.scan_id,
            "total_zombies": results.total_zombie_count,
            "total_monthly_savings": results.total_monthly_savings,
            "providers": [r.provider.value for r in results.results],
            "zombies": [z.model_dump(mode="json") for z in results.all_zombies],
        }
        console.print_json(json.dumps(output, default=str))

    elif format == "summary":
        console.print(results.get_summary())

    else:  # table
        if not results.all_zombies:
            console.print("[green]No zombie resources found! Your cloud is clean.[/green]")
            return

        table = Table(
            title=f"🧟 Zombie Resources Found (Scan: {results.scan_id})",
            show_header=True,
            header_style="bold red",
        )
        table.add_column("ID", style="cyan", max_width=30)
        table.add_column("Type")
        table.add_column("Provider")
        table.add_column("Region")
        table.add_column("Reason")
        table.add_column("Monthly Cost", justify="right", style="green")

        for zombie in results.all_zombies:
            table.add_row(
                zombie.id[:30],
                zombie.resource_type.value.replace("_", " ").title(),
                zombie.provider.value.upper(),
                zombie.region,
                zombie.reason.value.replace("_", " ").title(),
                f"${zombie.monthly_cost:.2f}",
            )

        console.print(table)
        console.print(
            f"\n[bold]Total: {results.total_zombie_count} zombies, "
            f"${results.total_monthly_savings:.2f}/month potential savings[/bold]"
        )


def main() -> None:
    """Main entry point."""
    cli(obj={})


if __name__ == "__main__":
    main()
