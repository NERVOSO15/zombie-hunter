"""Tests for resource type definitions."""

from datetime import datetime, timedelta

from zombie_hunter.resources.types import (
    AggregatedScanResult,
    CloudProvider,
    ResourceType,
    ScanResult,
    ZombieReason,
    ZombieResource,
)


class TestZombieResource:
    """Tests for ZombieResource model."""

    def test_create_zombie(self) -> None:
        """Test creating a zombie resource."""
        zombie = ZombieResource(
            id="vol-123",
            name="test-volume",
            provider=CloudProvider.AWS,
            resource_type=ResourceType.EBS_VOLUME,
            region="us-east-1",
            reason=ZombieReason.UNATTACHED,
            reason_detail="Not attached to any instance",
            size_gb=100,
            monthly_cost=8.0,
        )

        assert zombie.id == "vol-123"
        assert zombie.name == "test-volume"
        assert zombie.provider == CloudProvider.AWS
        assert zombie.resource_type == ResourceType.EBS_VOLUME
        assert zombie.monthly_cost == 8.0

    def test_display_name_with_name(self) -> None:
        """Test display name when name is set."""
        zombie = ZombieResource(
            id="vol-123",
            name="my-volume",
            provider=CloudProvider.AWS,
            resource_type=ResourceType.EBS_VOLUME,
            region="us-east-1",
            reason=ZombieReason.UNATTACHED,
        )

        assert zombie.display_name == "my-volume (vol-123)"

    def test_display_name_without_name(self) -> None:
        """Test display name when name is not set."""
        zombie = ZombieResource(
            id="vol-123",
            provider=CloudProvider.AWS,
            resource_type=ResourceType.EBS_VOLUME,
            region="us-east-1",
            reason=ZombieReason.UNATTACHED,
        )

        assert zombie.display_name == "vol-123"

    def test_age_days(self) -> None:
        """Test age calculation."""
        created = datetime.utcnow() - timedelta(days=30)
        zombie = ZombieResource(
            id="vol-123",
            provider=CloudProvider.AWS,
            resource_type=ResourceType.EBS_VOLUME,
            region="us-east-1",
            reason=ZombieReason.UNATTACHED,
            created_at=created,
        )

        assert zombie.age_days == 30

    def test_age_days_none(self) -> None:
        """Test age when created_at is not set."""
        zombie = ZombieResource(
            id="vol-123",
            provider=CloudProvider.AWS,
            resource_type=ResourceType.EBS_VOLUME,
            region="us-east-1",
            reason=ZombieReason.UNATTACHED,
        )

        assert zombie.age_days is None

    def test_idle_days(self) -> None:
        """Test idle days calculation."""
        last_used = datetime.utcnow() - timedelta(days=15)
        zombie = ZombieResource(
            id="lb-123",
            provider=CloudProvider.AWS,
            resource_type=ResourceType.ALB,
            region="us-east-1",
            reason=ZombieReason.NO_TRAFFIC,
            last_used_at=last_used,
        )

        assert zombie.idle_days == 15

    def test_to_slack_summary(self) -> None:
        """Test Slack summary generation."""
        zombie = ZombieResource(
            id="vol-123",
            provider=CloudProvider.AWS,
            resource_type=ResourceType.EBS_VOLUME,
            region="us-east-1",
            reason=ZombieReason.UNATTACHED,
            monthly_cost=40.0,
        )

        summary = zombie.to_slack_summary()
        assert "Ebs Volume" in summary
        assert "vol-123" in summary
        assert "us-east-1" in summary
        assert "$40.00" in summary

    def test_tags_and_metadata(self) -> None:
        """Test tags and metadata storage."""
        zombie = ZombieResource(
            id="vol-123",
            provider=CloudProvider.AWS,
            resource_type=ResourceType.EBS_VOLUME,
            region="us-east-1",
            reason=ZombieReason.UNATTACHED,
            tags={"Environment": "dev", "Team": "platform"},
            metadata={"volume_type": "gp3", "encrypted": True},
        )

        assert zombie.tags["Environment"] == "dev"
        assert zombie.metadata["volume_type"] == "gp3"
        assert zombie.metadata["encrypted"] is True


class TestScanResult:
    """Tests for ScanResult model."""

    def test_create_scan_result(self) -> None:
        """Test creating a scan result."""
        result = ScanResult(
            provider=CloudProvider.AWS,
            regions_scanned=["us-east-1", "us-west-2"],
        )

        assert result.provider == CloudProvider.AWS
        assert "us-east-1" in result.regions_scanned
        assert result.zombie_count == 0
        assert result.total_monthly_savings == 0.0

    def test_total_monthly_savings(self) -> None:
        """Test total savings calculation."""
        zombie1 = ZombieResource(
            id="vol-1",
            provider=CloudProvider.AWS,
            resource_type=ResourceType.EBS_VOLUME,
            region="us-east-1",
            reason=ZombieReason.UNATTACHED,
            monthly_cost=10.0,
        )
        zombie2 = ZombieResource(
            id="vol-2",
            provider=CloudProvider.AWS,
            resource_type=ResourceType.EBS_VOLUME,
            region="us-east-1",
            reason=ZombieReason.UNATTACHED,
            monthly_cost=20.0,
        )

        result = ScanResult(
            provider=CloudProvider.AWS,
            zombies=[zombie1, zombie2],
        )

        assert result.total_monthly_savings == 30.0
        assert result.zombie_count == 2

    def test_zombies_by_type(self) -> None:
        """Test grouping zombies by type."""
        volume = ZombieResource(
            id="vol-1",
            provider=CloudProvider.AWS,
            resource_type=ResourceType.EBS_VOLUME,
            region="us-east-1",
            reason=ZombieReason.UNATTACHED,
        )
        eip = ZombieResource(
            id="eip-1",
            provider=CloudProvider.AWS,
            resource_type=ResourceType.ELASTIC_IP,
            region="us-east-1",
            reason=ZombieReason.UNATTACHED,
        )

        result = ScanResult(
            provider=CloudProvider.AWS,
            zombies=[volume, eip],
        )

        by_type = result.zombies_by_type
        assert ResourceType.EBS_VOLUME in by_type
        assert ResourceType.ELASTIC_IP in by_type
        assert len(by_type[ResourceType.EBS_VOLUME]) == 1

    def test_mark_completed(self) -> None:
        """Test marking scan as completed."""
        result = ScanResult(provider=CloudProvider.AWS)
        assert result.scan_completed_at is None

        result.mark_completed()
        assert result.scan_completed_at is not None


class TestAggregatedScanResult:
    """Tests for AggregatedScanResult model."""

    def test_all_zombies(self) -> None:
        """Test aggregating zombies from multiple results."""
        aws_zombie = ZombieResource(
            id="vol-1",
            provider=CloudProvider.AWS,
            resource_type=ResourceType.EBS_VOLUME,
            region="us-east-1",
            reason=ZombieReason.UNATTACHED,
            monthly_cost=10.0,
        )
        gcp_zombie = ZombieResource(
            id="disk-1",
            provider=CloudProvider.GCP,
            resource_type=ResourceType.GCP_DISK,
            region="us-central1",
            reason=ZombieReason.UNATTACHED,
            monthly_cost=15.0,
        )

        aws_result = ScanResult(provider=CloudProvider.AWS, zombies=[aws_zombie])
        gcp_result = ScanResult(provider=CloudProvider.GCP, zombies=[gcp_zombie])

        aggregated = AggregatedScanResult(
            results=[aws_result, gcp_result],
            scan_id="test-123",
        )

        assert aggregated.total_zombie_count == 2
        assert aggregated.total_monthly_savings == 25.0
        assert len(aggregated.all_zombies) == 2

    def test_providers_scanned(self) -> None:
        """Test listing scanned providers."""
        aws_result = ScanResult(provider=CloudProvider.AWS)
        gcp_result = ScanResult(provider=CloudProvider.GCP)

        aggregated = AggregatedScanResult(results=[aws_result, gcp_result])

        providers = aggregated.providers_scanned
        assert CloudProvider.AWS in providers
        assert CloudProvider.GCP in providers

    def test_get_summary(self) -> None:
        """Test summary generation."""
        zombie = ZombieResource(
            id="vol-1",
            provider=CloudProvider.AWS,
            resource_type=ResourceType.EBS_VOLUME,
            region="us-east-1",
            reason=ZombieReason.UNATTACHED,
            monthly_cost=50.0,
        )

        result = ScanResult(provider=CloudProvider.AWS, zombies=[zombie])
        aggregated = AggregatedScanResult(results=[result], scan_id="test-123")

        summary = aggregated.get_summary()
        assert "test-123" in summary
        assert "aws" in summary.lower()
        assert "1" in summary
        assert "50" in summary
