"""Resource type definitions for zombie resources."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class CloudProvider(str, Enum):
    """Supported cloud providers."""

    AWS = "aws"
    GCP = "gcp"
    AZURE = "azure"


class ResourceType(str, Enum):
    """Types of cloud resources that can be zombies."""

    # Storage
    EBS_VOLUME = "ebs_volume"
    GCP_DISK = "gcp_disk"
    AZURE_DISK = "azure_disk"

    # Networking
    ELASTIC_IP = "elastic_ip"
    GCP_STATIC_IP = "gcp_static_ip"
    AZURE_PUBLIC_IP = "azure_public_ip"

    # Load Balancers
    ALB = "alb"
    NLB = "nlb"
    CLB = "clb"
    GCP_LOAD_BALANCER = "gcp_load_balancer"
    AZURE_LOAD_BALANCER = "azure_load_balancer"

    # Snapshots
    EBS_SNAPSHOT = "ebs_snapshot"
    RDS_SNAPSHOT = "rds_snapshot"
    GCP_SNAPSHOT = "gcp_snapshot"
    AZURE_SNAPSHOT = "azure_snapshot"

    # Other
    UNATTACHED_ENI = "unattached_eni"
    UNUSED_NAT_GATEWAY = "unused_nat_gateway"


class ZombieReason(str, Enum):
    """Reasons why a resource is considered a zombie."""

    UNATTACHED = "unattached"
    NO_TRAFFIC = "no_traffic"
    NO_TARGETS = "no_targets"
    AGE_EXCEEDED = "age_exceeded"
    UNUSED = "unused"
    ORPHANED = "orphaned"


class ZombieResource(BaseModel):
    """Represents a zombie cloud resource."""

    # Identification
    id: str = Field(..., description="Resource ID")
    name: str = Field(default="", description="Resource name/tag")
    provider: CloudProvider = Field(..., description="Cloud provider")
    resource_type: ResourceType = Field(..., description="Type of resource")
    region: str = Field(..., description="Region/zone where resource exists")

    # Zombie details
    reason: ZombieReason = Field(..., description="Why this is a zombie")
    reason_detail: str = Field(default="", description="Detailed explanation")

    # Cost information
    monthly_cost: float = Field(default=0.0, ge=0, description="Estimated monthly cost (USD)")
    size_gb: float | None = Field(default=None, description="Size in GB if applicable")

    # Timestamps
    created_at: datetime | None = Field(default=None, description="When resource was created")
    last_used_at: datetime | None = Field(default=None, description="When resource was last used")
    discovered_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="When zombie was discovered",
    )

    # Metadata
    tags: dict[str, str] = Field(default_factory=dict, description="Resource tags")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional provider-specific metadata",
    )

    # Deletion status
    can_delete: bool = Field(
        default=True,
        description="Whether this resource can be safely deleted",
    )
    deletion_warning: str | None = Field(
        default=None,
        description="Warning message if deletion might be risky",
    )

    @property
    def display_name(self) -> str:
        """Get a display-friendly name for the resource."""
        if self.name:
            return f"{self.name} ({self.id})"
        return self.id

    @property
    def age_days(self) -> int | None:
        """Get the age of the resource in days."""
        if self.created_at is None:
            return None
        delta = datetime.utcnow() - self.created_at
        return delta.days

    @property
    def idle_days(self) -> int | None:
        """Get number of days since last use."""
        if self.last_used_at is None:
            return None
        delta = datetime.utcnow() - self.last_used_at
        return delta.days

    def to_slack_summary(self) -> str:
        """Generate a short summary for Slack."""
        return (
            f"*{self.resource_type.value.replace('_', ' ').title()}*\n"
            f"ID: `{self.id}`\n"
            f"Region: {self.region}\n"
            f"Monthly Cost: ${self.monthly_cost:.2f}"
        )


class ScanResult(BaseModel):
    """Result of scanning a cloud provider."""

    provider: CloudProvider = Field(..., description="Cloud provider scanned")
    regions_scanned: list[str] = Field(default_factory=list, description="Regions scanned")
    zombies: list[ZombieResource] = Field(default_factory=list, description="Found zombies")
    scan_started_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="When scan started",
    )
    scan_completed_at: datetime | None = Field(default=None, description="When scan completed")
    errors: list[str] = Field(default_factory=list, description="Errors during scan")

    @property
    def total_monthly_savings(self) -> float:
        """Calculate total potential monthly savings."""
        return sum(z.monthly_cost for z in self.zombies)

    @property
    def zombie_count(self) -> int:
        """Get total number of zombies found."""
        return len(self.zombies)

    @property
    def zombies_by_type(self) -> dict[ResourceType, list[ZombieResource]]:
        """Group zombies by resource type."""
        result: dict[ResourceType, list[ZombieResource]] = {}
        for zombie in self.zombies:
            if zombie.resource_type not in result:
                result[zombie.resource_type] = []
            result[zombie.resource_type].append(zombie)
        return result

    def mark_completed(self) -> None:
        """Mark the scan as completed."""
        self.scan_completed_at = datetime.utcnow()


class AggregatedScanResult(BaseModel):
    """Aggregated results from multiple providers."""

    results: list[ScanResult] = Field(default_factory=list, description="Individual scan results")
    scan_id: str = Field(default="", description="Unique scan identifier")

    @property
    def all_zombies(self) -> list[ZombieResource]:
        """Get all zombies from all providers."""
        zombies = []
        for result in self.results:
            zombies.extend(result.zombies)
        return zombies

    @property
    def total_monthly_savings(self) -> float:
        """Calculate total potential monthly savings across all providers."""
        return sum(r.total_monthly_savings for r in self.results)

    @property
    def total_zombie_count(self) -> int:
        """Get total number of zombies across all providers."""
        return sum(r.zombie_count for r in self.results)

    @property
    def providers_scanned(self) -> list[CloudProvider]:
        """Get list of providers that were scanned."""
        return [r.provider for r in self.results]

    def get_summary(self) -> str:
        """Generate a summary of the scan results."""
        lines = [
            f"Scan ID: {self.scan_id}",
            f"Providers: {', '.join(p.value for p in self.providers_scanned)}",
            f"Total Zombies: {self.total_zombie_count}",
            f"Potential Monthly Savings: ${self.total_monthly_savings:.2f}",
            "",
            "Breakdown by provider:",
        ]

        for result in self.results:
            lines.append(
                f"  - {result.provider.value}: {result.zombie_count} zombies "
                f"(${result.total_monthly_savings:.2f}/month)"
            )

        return "\n".join(lines)
