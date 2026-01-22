"""Mock scanner for testing without cloud accounts."""

import random
from datetime import datetime, timedelta
from typing import Any

import structlog

from zombie_hunter.config import Settings
from zombie_hunter.cost.estimator import CostEstimator
from zombie_hunter.resources.types import (
    CloudProvider,
    ResourceType,
    ZombieReason,
    ZombieResource,
)
from zombie_hunter.scanners.base import BaseScanner, ScannerRegistry

logger = structlog.get_logger()


# Sample data for generating realistic mock resources
MOCK_VOLUME_NAMES = [
    "old-jenkins-data",
    "deprecated-app-storage",
    "test-volume-deleteme",
    "backup-temp-2023",
    "dev-scratch-disk",
    "migration-leftover",
    "unused-database-vol",
    "orphaned-pvc",
]

MOCK_LB_NAMES = [
    "legacy-api-lb",
    "old-frontend-alb",
    "deprecated-service-lb",
    "test-load-balancer",
    "staging-lb-unused",
    "migration-temp-lb",
]

MOCK_SNAPSHOT_NAMES = [
    "pre-migration-backup",
    "old-prod-snapshot",
    "test-db-snapshot-2023",
    "deprecated-backup",
    "manual-snapshot-deleteme",
    "emergency-backup-jan",
]

MOCK_REGIONS = ["us-east-1", "us-west-2", "eu-west-1"]


class MockScanner(BaseScanner):
    """
    Mock scanner that generates fake zombie resources for testing.

    This scanner doesn't connect to any cloud provider - it generates
    realistic-looking zombie resources for testing the full workflow.
    """

    def __init__(self, settings: Settings) -> None:
        """Initialize mock scanner."""
        # Don't call super().__init__ to avoid provider validation
        self.settings = settings
        self.dry_run = settings.dry_run
        self.cost_estimator = CostEstimator()
        self._log = logger.bind(provider="mock")
        self._deleted_resources: set[str] = set()

    @property
    def provider(self) -> CloudProvider:
        """Return AWS as the mock provider (for compatibility)."""
        return CloudProvider.AWS

    @property
    def regions(self) -> list[str]:
        """Return mock regions."""
        return MOCK_REGIONS

    def scan_volumes(self, region: str) -> list[ZombieResource]:
        """Generate mock unattached volumes."""
        zombies: list[ZombieResource] = []
        num_volumes = random.randint(1, 4)

        for _ in range(num_volumes):
            name = random.choice(MOCK_VOLUME_NAMES)
            size = random.choice([20, 50, 100, 200, 500, 1000])
            volume_type = random.choice(["gp2", "gp3", "io1", "st1"])
            days_old = random.randint(30, 365)

            zombie = ZombieResource(
                id=f"vol-{random.randint(10000000, 99999999):08x}",
                name=name,
                provider=CloudProvider.AWS,
                resource_type=ResourceType.EBS_VOLUME,
                region=region,
                reason=ZombieReason.UNATTACHED,
                reason_detail="Volume is not attached to any instance",
                size_gb=size,
                created_at=datetime.utcnow() - timedelta(days=days_old),
                tags={
                    "Environment": random.choice(["dev", "staging", "test"]),
                    "Team": random.choice(["platform", "backend", "data"]),
                },
                metadata={
                    "volume_type": volume_type,
                    "encrypted": random.choice([True, False]),
                },
            )

            self.cost_estimator.update_resource_cost(zombie)
            zombies.append(zombie)

        return zombies

    def scan_ips(self, region: str) -> list[ZombieResource]:
        """Generate mock unattached Elastic IPs."""
        zombies: list[ZombieResource] = []
        num_ips = random.randint(0, 3)

        for _ in range(num_ips):
            days_old = random.randint(7, 180)

            zombie = ZombieResource(
                id=f"eipalloc-{random.randint(10000000, 99999999):08x}",
                name="",
                provider=CloudProvider.AWS,
                resource_type=ResourceType.ELASTIC_IP,
                region=region,
                reason=ZombieReason.UNATTACHED,
                reason_detail="Elastic IP is not associated with any resource",
                created_at=datetime.utcnow() - timedelta(days=days_old),
                metadata={
                    "public_ip": (
                        f"{random.randint(1, 255)}.{random.randint(1, 255)}."
                        f"{random.randint(1, 255)}.{random.randint(1, 255)}"
                    ),
                    "domain": "vpc",
                },
            )

            self.cost_estimator.update_resource_cost(zombie)
            zombies.append(zombie)

        return zombies

    def scan_load_balancers(self, region: str) -> list[ZombieResource]:
        """Generate mock idle load balancers."""
        zombies: list[ZombieResource] = []
        num_lbs = random.randint(0, 2)

        for _ in range(num_lbs):
            name = random.choice(MOCK_LB_NAMES)
            lb_type = random.choice(["application", "network"])
            days_old = random.randint(60, 300)
            has_targets = random.choice([True, False])

            resource_type = ResourceType.ALB if lb_type == "application" else ResourceType.NLB
            reason = ZombieReason.NO_TARGETS if not has_targets else ZombieReason.NO_TRAFFIC
            reason_detail = (
                "Load balancer has no registered targets"
                if not has_targets
                else f"No traffic in the last {self.settings.thresholds.lb_idle_days} days"
            )

            zombie = ZombieResource(
                id=(
                    f"arn:aws:elasticloadbalancing:{region}:123456789:"
                    f"loadbalancer/{lb_type}/{name}/{random.randint(1000000, 9999999)}"
                ),
                name=name,
                provider=CloudProvider.AWS,
                resource_type=resource_type,
                region=region,
                reason=reason,
                reason_detail=reason_detail,
                created_at=datetime.utcnow() - timedelta(days=days_old),
                metadata={
                    "dns_name": f"{name}-{random.randint(100000, 999999)}.{region}.elb.amazonaws.com",
                    "scheme": random.choice(["internet-facing", "internal"]),
                    "type": lb_type,
                    "has_targets": has_targets,
                    "has_traffic": False,
                },
            )

            self.cost_estimator.update_resource_cost(zombie)
            zombies.append(zombie)

        return zombies

    def scan_snapshots(self, region: str) -> list[ZombieResource]:
        """Generate mock old RDS snapshots."""
        zombies: list[ZombieResource] = []
        num_snapshots = random.randint(1, 5)

        for _ in range(num_snapshots):
            name = random.choice(MOCK_SNAPSHOT_NAMES)
            size = random.choice([20, 50, 100, 200, 500])
            days_old = random.randint(
                self.settings.thresholds.snapshot_age_days + 1,
                self.settings.thresholds.snapshot_age_days + 180,
            )
            db_exists = random.choice([True, False])

            zombie = ZombieResource(
                id=f"rds:{name}-{random.randint(1000, 9999)}",
                name=f"{name}-{random.randint(1000, 9999)}",
                provider=CloudProvider.AWS,
                resource_type=ResourceType.RDS_SNAPSHOT,
                region=region,
                reason=ZombieReason.AGE_EXCEEDED,
                reason_detail=(
                    f"Snapshot is older than {self.settings.thresholds.snapshot_age_days} days"
                ),
                size_gb=size,
                created_at=datetime.utcnow() - timedelta(days=days_old),
                metadata={
                    "engine": random.choice(["mysql", "postgres", "aurora-mysql"]),
                    "engine_version": random.choice(["8.0.28", "14.6", "5.7"]),
                    "status": "available",
                    "encrypted": random.choice([True, False]),
                },
            )

            if not db_exists:
                zombie.deletion_warning = (
                    "Source database no longer exists - this may be the only backup"
                )

            self.cost_estimator.update_resource_cost(zombie)
            zombies.append(zombie)

        return zombies

    def delete_resource(self, resource: ZombieResource) -> bool:
        """Simulate deleting a resource."""
        self._log.info(
            "mock_delete",
            resource_id=resource.id,
            resource_type=resource.resource_type.value,
        )
        self._deleted_resources.add(resource.id)
        return True

    def get_resource_details(self, resource: ZombieResource) -> dict[str, Any]:
        """Get mock resource details."""
        return {
            "id": resource.id,
            "name": resource.name,
            "type": resource.resource_type.value,
            "region": resource.region,
            "reason": resource.reason.value,
            "reason_detail": resource.reason_detail,
            "monthly_cost": f"${resource.monthly_cost:.2f}",
            "created_at": resource.created_at.isoformat() if resource.created_at else None,
            "tags": resource.tags,
            "note": "This is mock data for testing purposes",
            **resource.metadata,
        }


def register_mock_scanner() -> None:
    """Register the mock scanner for the AWS provider (overrides real scanner)."""
    ScannerRegistry._scanners[CloudProvider.AWS] = MockScanner
