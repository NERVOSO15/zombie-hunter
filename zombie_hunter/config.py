"""Configuration management for Zombie Hunter using pydantic-settings."""

from enum import Enum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CloudProvider(str, Enum):
    """Supported cloud providers."""

    AWS = "aws"
    GCP = "gcp"
    AZURE = "azure"


class SlackMode(str, Enum):
    """Slack notification modes."""

    INTERACTIVE = "interactive"
    REPORT_ONLY = "report-only"


class ScannerSettings(BaseSettings):
    """Scanner-specific settings."""

    enabled_providers: list[CloudProvider] = Field(
        default=[CloudProvider.AWS],
        description="List of cloud providers to scan",
    )
    aws_regions: list[str] = Field(
        default=["us-east-1"],
        description="AWS regions to scan",
    )
    gcp_regions: list[str] = Field(
        default=["us-central1"],
        description="GCP regions to scan",
    )
    azure_regions: list[str] = Field(
        default=["eastus"],
        description="Azure regions to scan",
    )


class ThresholdSettings(BaseSettings):
    """Threshold settings for zombie detection."""

    snapshot_age_days: int = Field(
        default=90,
        ge=1,
        description="RDS snapshots older than this are zombies",
    )
    lb_idle_days: int = Field(
        default=30,
        ge=1,
        description="Load balancers idle for this many days are zombies",
    )
    min_cost_threshold: float = Field(
        default=1.0,
        ge=0,
        description="Minimum monthly cost to report (USD)",
    )


class SlackSettings(BaseSettings):
    """Slack integration settings."""

    model_config = SettingsConfigDict(
        env_prefix="SLACK_",
    )

    bot_token: str = Field(
        default="",
        description="Slack bot token (xoxb-...)",
    )
    signing_secret: str = Field(
        default="",
        description="Slack signing secret for webhook validation",
    )
    channel: str = Field(
        default="#finops-alerts",
        description="Slack channel for notifications",
    )
    mode: SlackMode = Field(
        default=SlackMode.INTERACTIVE,
        description="Notification mode",
    )
    post_individual_resources: bool = Field(
        default=True,
        description="Post individual resource notifications",
    )
    max_individual_posts: int = Field(
        default=20,
        ge=1,
        description="Max resources to post individually",
    )


class LoggingSettings(BaseSettings):
    """Logging configuration."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="Log level",
    )
    format: Literal["json", "console"] = Field(
        default="json",
        description="Log output format",
    )


class AWSCredentials(BaseSettings):
    """AWS credential settings."""

    model_config = SettingsConfigDict(
        env_prefix="AWS_",
    )

    access_key_id: str = Field(default="", description="AWS access key ID")
    secret_access_key: str = Field(default="", description="AWS secret access key")
    default_region: str = Field(default="us-east-1", description="Default AWS region")


class GCPCredentials(BaseSettings):
    """GCP credential settings."""

    model_config = SettingsConfigDict(
        env_prefix="GCP_",
    )

    project_id: str = Field(default="", description="GCP project ID")
    credentials_path: str = Field(
        default="",
        alias="GOOGLE_APPLICATION_CREDENTIALS",
        description="Path to service account JSON",
    )


class AzureCredentials(BaseSettings):
    """Azure credential settings."""

    model_config = SettingsConfigDict(
        env_prefix="AZURE_",
    )

    subscription_id: str = Field(default="", description="Azure subscription ID")
    tenant_id: str = Field(default="", description="Azure tenant ID")
    client_id: str = Field(default="", description="Azure client ID")
    client_secret: str = Field(default="", description="Azure client secret")


class Settings(BaseSettings):
    """Main application settings."""

    model_config = SettingsConfigDict(
        env_prefix="ZOMBIE_HUNTER_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Configuration file path
    config_path: Path | None = Field(
        default=None,
        description="Path to YAML configuration file",
    )

    # Dry run mode
    dry_run: bool = Field(
        default=True,
        description="If true, no actual deletions occur",
    )

    # Nested settings
    scanner: ScannerSettings = Field(default_factory=ScannerSettings)
    thresholds: ThresholdSettings = Field(default_factory=ThresholdSettings)
    slack: SlackSettings = Field(default_factory=SlackSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    # Cloud credentials
    aws: AWSCredentials = Field(default_factory=AWSCredentials)
    gcp: GCPCredentials = Field(default_factory=GCPCredentials)
    azure: AzureCredentials = Field(default_factory=AzureCredentials)

    @field_validator("config_path", mode="before")
    @classmethod
    def resolve_config_path(cls, v: str | Path | None) -> Path | None:
        """Resolve configuration file path."""
        if v is None:
            return None
        path = Path(v)
        if not path.is_absolute():
            path = Path.cwd() / path
        return path

    def load_from_yaml(self) -> "Settings":
        """Load settings from YAML file and merge with environment variables."""
        if self.config_path is None or not self.config_path.exists():
            return self

        with open(self.config_path) as f:
            yaml_config = yaml.safe_load(f) or {}

        # Merge YAML config with current settings
        return self._merge_yaml_config(yaml_config)

    def _merge_yaml_config(self, yaml_config: dict) -> "Settings":
        """Merge YAML configuration with current settings."""
        updates = {}

        if "scanner" in yaml_config:
            scanner_data = yaml_config["scanner"]
            updates["scanner"] = ScannerSettings(
                enabled_providers=scanner_data.get(
                    "enabled_providers", self.scanner.enabled_providers
                ),
                aws_regions=scanner_data.get("aws_regions", self.scanner.aws_regions),
                gcp_regions=scanner_data.get("gcp_regions", self.scanner.gcp_regions),
                azure_regions=scanner_data.get("azure_regions", self.scanner.azure_regions),
            )

        if "thresholds" in yaml_config:
            thresh_data = yaml_config["thresholds"]
            updates["thresholds"] = ThresholdSettings(
                snapshot_age_days=thresh_data.get(
                    "snapshot_age_days", self.thresholds.snapshot_age_days
                ),
                lb_idle_days=thresh_data.get("lb_idle_days", self.thresholds.lb_idle_days),
                min_cost_threshold=thresh_data.get(
                    "min_cost_threshold", self.thresholds.min_cost_threshold
                ),
            )

        if "slack" in yaml_config:
            slack_data = yaml_config["slack"]
            updates["slack"] = SlackSettings(
                mode=slack_data.get("mode", self.slack.mode),
                channel=slack_data.get("channel", self.slack.channel),
                post_individual_resources=slack_data.get(
                    "post_individual_resources", self.slack.post_individual_resources
                ),
                max_individual_posts=slack_data.get(
                    "max_individual_posts", self.slack.max_individual_posts
                ),
                # Keep tokens from env vars
                bot_token=self.slack.bot_token,
                signing_secret=self.slack.signing_secret,
            )

        if "logging" in yaml_config:
            log_data = yaml_config["logging"]
            updates["logging"] = LoggingSettings(
                level=log_data.get("level", self.logging.level),
                format=log_data.get("format", self.logging.format),
            )

        if "dry_run" in yaml_config:
            updates["dry_run"] = yaml_config["dry_run"]

        return self.model_copy(update=updates)


def get_settings(config_path: Path | None = None) -> Settings:
    """
    Get application settings.

    Settings are loaded in the following order (later sources override earlier):
    1. Default values
    2. YAML configuration file (if provided)
    3. Environment variables

    Args:
        config_path: Optional path to YAML configuration file

    Returns:
        Configured Settings instance
    """
    # Load base settings from environment
    settings = Settings()

    # Override config path if provided
    if config_path is not None:
        settings = settings.model_copy(update={"config_path": config_path})

    # Merge YAML configuration
    settings = settings.load_from_yaml()

    return settings


# Global settings instance (lazy initialization)
_settings: Settings | None = None


def get_global_settings() -> Settings:
    """Get the global settings instance."""
    global _settings
    if _settings is None:
        _settings = get_settings()
    return _settings


def init_settings(config_path: Path | None = None) -> Settings:
    """Initialize global settings with optional config path."""
    global _settings
    _settings = get_settings(config_path)
    return _settings
