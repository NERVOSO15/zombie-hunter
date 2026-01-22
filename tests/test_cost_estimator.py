"""Tests for the cost estimator module."""

import pytest

from zombie_hunter.cost.estimator import HOURS_PER_MONTH, CostEstimator
from zombie_hunter.resources.types import (
    CloudProvider,
    ResourceType,
    ZombieReason,
    ZombieResource,
)


@pytest.fixture
def estimator() -> CostEstimator:
    """Create a cost estimator instance."""
    return CostEstimator()


@pytest.fixture
def ebs_volume() -> ZombieResource:
    """Create an EBS volume zombie resource."""
    return ZombieResource(
        id="vol-0abc123def",
        name="test-volume",
        provider=CloudProvider.AWS,
        resource_type=ResourceType.EBS_VOLUME,
        region="us-east-1",
        reason=ZombieReason.UNATTACHED,
        reason_detail="Volume is not attached",
        size_gb=100,
        metadata={"volume_type": "gp3"},
    )


@pytest.fixture
def elastic_ip() -> ZombieResource:
    """Create an Elastic IP zombie resource."""
    return ZombieResource(
        id="eipalloc-0abc123",
        provider=CloudProvider.AWS,
        resource_type=ResourceType.ELASTIC_IP,
        region="us-east-1",
        reason=ZombieReason.UNATTACHED,
        reason_detail="EIP not associated",
    )


class TestCostEstimator:
    """Tests for CostEstimator class."""

    def test_ebs_volume_cost(self, estimator: CostEstimator, ebs_volume: ZombieResource) -> None:
        """Test EBS volume cost estimation."""
        cost = estimator.estimate_monthly_cost(ebs_volume)
        # 100 GB * $0.08/GB = $8.00
        assert cost == pytest.approx(8.0, rel=0.01)

    def test_elastic_ip_cost(self, estimator: CostEstimator, elastic_ip: ZombieResource) -> None:
        """Test Elastic IP cost estimation."""
        cost = estimator.estimate_monthly_cost(elastic_ip)
        # $0.005/hour * 730 hours = $3.65
        expected = 0.005 * HOURS_PER_MONTH
        assert cost == pytest.approx(expected, rel=0.01)

    def test_update_resource_cost(
        self, estimator: CostEstimator, ebs_volume: ZombieResource
    ) -> None:
        """Test updating resource cost in place."""
        assert ebs_volume.monthly_cost == 0.0
        updated = estimator.update_resource_cost(ebs_volume)
        assert updated.monthly_cost == pytest.approx(8.0, rel=0.01)
        assert ebs_volume.monthly_cost == pytest.approx(8.0, rel=0.01)

    def test_total_savings(
        self,
        estimator: CostEstimator,
        ebs_volume: ZombieResource,
        elastic_ip: ZombieResource,
    ) -> None:
        """Test total savings calculation."""
        estimator.update_resource_cost(ebs_volume)
        estimator.update_resource_cost(elastic_ip)

        resources = [ebs_volume, elastic_ip]
        total = estimator.get_total_savings(resources)

        expected = 8.0 + (0.005 * HOURS_PER_MONTH)
        assert total == pytest.approx(expected, rel=0.01)

    def test_annual_savings(self, estimator: CostEstimator, ebs_volume: ZombieResource) -> None:
        """Test annual savings calculation."""
        estimator.update_resource_cost(ebs_volume)

        annual = estimator.get_annual_savings([ebs_volume])
        assert annual == pytest.approx(8.0 * 12, rel=0.01)

    def test_cost_breakdown(
        self,
        estimator: CostEstimator,
        ebs_volume: ZombieResource,
        elastic_ip: ZombieResource,
    ) -> None:
        """Test cost breakdown by resource type."""
        estimator.update_resource_cost(ebs_volume)
        estimator.update_resource_cost(elastic_ip)

        resources = [ebs_volume, elastic_ip]
        breakdown = estimator.get_cost_breakdown(resources)

        assert ResourceType.EBS_VOLUME in breakdown
        assert breakdown[ResourceType.EBS_VOLUME]["count"] == 1
        assert breakdown[ResourceType.EBS_VOLUME]["monthly_cost"] == pytest.approx(8.0, rel=0.01)

        assert ResourceType.ELASTIC_IP in breakdown
        assert breakdown[ResourceType.ELASTIC_IP]["count"] == 1

    def test_format_cost(self, estimator: CostEstimator) -> None:
        """Test cost formatting."""
        assert estimator.format_cost(8.0) == "$8.00"
        assert estimator.format_cost(1234.56) == "$1,235"
        assert estimator.format_cost(0.5) == "$0.50"

    def test_custom_pricing(self) -> None:
        """Test custom pricing override."""
        custom_pricing = {"ebs_gp3_per_gb": 0.10}
        estimator = CostEstimator(aws_pricing=custom_pricing)

        volume = ZombieResource(
            id="vol-test",
            provider=CloudProvider.AWS,
            resource_type=ResourceType.EBS_VOLUME,
            region="us-east-1",
            reason=ZombieReason.UNATTACHED,
            size_gb=100,
            metadata={"volume_type": "gp3"},
        )

        cost = estimator.estimate_monthly_cost(volume)
        # 100 GB * $0.10/GB = $10.00
        assert cost == pytest.approx(10.0, rel=0.01)


class TestGCPCostEstimation:
    """Tests for GCP cost estimation."""

    def test_gcp_disk_cost(self, estimator: CostEstimator) -> None:
        """Test GCP disk cost estimation."""
        disk = ZombieResource(
            id="disk-test",
            provider=CloudProvider.GCP,
            resource_type=ResourceType.GCP_DISK,
            region="us-central1-a",
            reason=ZombieReason.UNATTACHED,
            size_gb=100,
            metadata={"disk_type": "pd-ssd"},
        )

        cost = estimator.estimate_monthly_cost(disk)
        # 100 GB * $0.17/GB = $17.00
        assert cost == pytest.approx(17.0, rel=0.01)

    def test_gcp_static_ip_cost(self, estimator: CostEstimator) -> None:
        """Test GCP static IP cost estimation."""
        ip = ZombieResource(
            id="ip-test",
            provider=CloudProvider.GCP,
            resource_type=ResourceType.GCP_STATIC_IP,
            region="us-central1",
            reason=ZombieReason.UNATTACHED,
        )

        cost = estimator.estimate_monthly_cost(ip)
        # $0.01/hour * 730 hours = $7.30
        expected = 0.01 * HOURS_PER_MONTH
        assert cost == pytest.approx(expected, rel=0.01)


class TestAzureCostEstimation:
    """Tests for Azure cost estimation."""

    def test_azure_disk_cost(self, estimator: CostEstimator) -> None:
        """Test Azure managed disk cost estimation."""
        disk = ZombieResource(
            id="disk-test",
            provider=CloudProvider.AZURE,
            resource_type=ResourceType.AZURE_DISK,
            region="eastus",
            reason=ZombieReason.UNATTACHED,
            size_gb=128,
            metadata={"disk_type": "Standard_SSD"},
        )

        cost = estimator.estimate_monthly_cost(disk)
        # 128 GB * $0.075/GB = $9.60
        assert cost == pytest.approx(9.6, rel=0.01)

    def test_azure_public_ip_cost(self, estimator: CostEstimator) -> None:
        """Test Azure public IP cost estimation."""
        ip = ZombieResource(
            id="ip-test",
            provider=CloudProvider.AZURE,
            resource_type=ResourceType.AZURE_PUBLIC_IP,
            region="eastus",
            reason=ZombieReason.UNATTACHED,
        )

        cost = estimator.estimate_monthly_cost(ip)
        # $0.005/hour * 730 hours = $3.65
        expected = 0.005 * HOURS_PER_MONTH
        assert cost == pytest.approx(expected, rel=0.01)
