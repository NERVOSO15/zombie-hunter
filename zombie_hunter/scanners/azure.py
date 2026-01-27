"""Azure scanner for detecting zombie resources."""

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

try:
    from azure.core.exceptions import AzureError, ResourceNotFoundError
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.network import NetworkManagementClient

    AZURE_AVAILABLE = True
except ImportError:
    AZURE_AVAILABLE = False

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


@ScannerRegistry.register(CloudProvider.AZURE)
class AzureScanner(BaseScanner):
    """
    Scanner for Azure zombie resources.

    This scanner detects:
    - Unattached Managed Disks
    - Unused Public IPs
    - Empty Load Balancers
    - Old Snapshots

    All scan methods are synchronous internally and wrapped by the base class
    using asyncio.to_thread() for non-blocking async execution.
    """

    def __init__(self, settings: Settings) -> None:
        """Initialize Azure scanner."""
        if not AZURE_AVAILABLE:
            raise ImportError(
                "Azure SDK not installed. Install with: "
                "pip install azure-identity azure-mgmt-compute azure-mgmt-network"
            )

        super().__init__(settings)
        self.cost_estimator = CostEstimator()
        self.subscription_id = settings.azure.subscription_id

        if not self.subscription_id:
            raise ValueError(
                "Azure subscription ID is required. Set AZURE_SUBSCRIPTION_ID environment variable."
            )

        # Initialize credentials and clients
        self._credential = DefaultAzureCredential()
        self._compute_client = ComputeManagementClient(self._credential, self.subscription_id)
        self._network_client = NetworkManagementClient(self._credential, self.subscription_id)

    @property
    def provider(self) -> CloudProvider:
        """Return Azure as the cloud provider."""
        return CloudProvider.AZURE

    @property
    def regions(self) -> list[str]:
        """Return configured Azure regions."""
        return self.settings.scanner.azure_regions

    # -------------------------------------------------------------------------
    # Synchronous scan implementations (wrapped by base class for async)
    # -------------------------------------------------------------------------

    def _scan_volumes_sync(self, region: str) -> list[ZombieResource]:
        """Scan for unattached managed disks (synchronous)."""
        zombies: list[ZombieResource] = []

        try:
            # List all disks in the subscription
            for disk in self._compute_client.disks.list():
                # Filter by region
                if disk.location.lower() != region.lower():
                    continue

                # Check if disk is unattached (no managed_by)
                if disk.disk_state == "Unattached" or not disk.managed_by:
                    # Get disk type from SKU
                    disk_type = "Standard_HDD"
                    if disk.sku:
                        disk_type = disk.sku.name

                    # Parse resource group from ID
                    resource_group = self._get_resource_group(disk.id)

                    zombie = ZombieResource(
                        id=disk.name,
                        name=disk.name,
                        provider=CloudProvider.AZURE,
                        resource_type=ResourceType.AZURE_DISK,
                        region=disk.location,
                        reason=ZombieReason.UNATTACHED,
                        reason_detail="Disk is not attached to any virtual machine",
                        size_gb=disk.disk_size_gb,
                        created_at=(
                            disk.time_created.replace(tzinfo=None) if disk.time_created else None
                        ),
                        metadata={
                            "disk_type": disk_type,
                            "disk_state": disk.disk_state,
                            "resource_group": resource_group,
                            "provisioning_state": disk.provisioning_state,
                            "os_type": disk.os_type if disk.os_type else None,
                            "resource_id": disk.id,
                        },
                    )

                    self.cost_estimator.update_resource_cost(zombie)
                    zombies.append(zombie)

                    self._log.debug(
                        "found_zombie_disk",
                        disk_name=disk.name,
                        size_gb=disk.disk_size_gb,
                        region=disk.location,
                    )

        except AzureError as e:
            self._log.error("scan_volumes_error", region=region, error=str(e))
            raise

        return zombies

    def _scan_ips_sync(self, region: str) -> list[ZombieResource]:
        """Scan for unassociated public IP addresses (synchronous)."""
        zombies: list[ZombieResource] = []

        try:
            for public_ip in self._network_client.public_ip_addresses.list_all():
                # Filter by region
                if public_ip.location.lower() != region.lower():
                    continue

                # Check if IP is not associated with anything
                if not public_ip.ip_configuration:
                    resource_group = self._get_resource_group(public_ip.id)

                    zombie = ZombieResource(
                        id=public_ip.name,
                        name=public_ip.name,
                        provider=CloudProvider.AZURE,
                        resource_type=ResourceType.AZURE_PUBLIC_IP,
                        region=public_ip.location,
                        reason=ZombieReason.UNATTACHED,
                        reason_detail="Public IP is not associated with any resource",
                        metadata={
                            "ip_address": public_ip.ip_address,
                            "allocation_method": public_ip.public_ip_allocation_method,
                            "sku": public_ip.sku.name if public_ip.sku else None,
                            "resource_group": resource_group,
                            "provisioning_state": public_ip.provisioning_state,
                            "resource_id": public_ip.id,
                        },
                    )

                    self.cost_estimator.update_resource_cost(zombie)
                    zombies.append(zombie)

                    self._log.debug(
                        "found_zombie_ip",
                        ip_name=public_ip.name,
                        ip_address=public_ip.ip_address,
                    )

        except AzureError as e:
            self._log.error("scan_ips_error", region=region, error=str(e))
            raise

        return zombies

    def _scan_load_balancers_sync(self, region: str) -> list[ZombieResource]:
        """Scan for load balancers with no backend pools or rules (synchronous)."""
        zombies: list[ZombieResource] = []

        try:
            for lb in self._network_client.load_balancers.list_all():
                # Filter by region
                if lb.location.lower() != region.lower():
                    continue

                # Check for empty backend pools or no load balancing rules
                has_backends = bool(lb.backend_address_pools) and any(
                    pool.load_balancer_backend_addresses
                    for pool in lb.backend_address_pools
                    if hasattr(pool, "load_balancer_backend_addresses")
                )
                has_rules = bool(lb.load_balancing_rules)

                is_zombie = False
                reason_detail = ""

                if not has_backends:
                    is_zombie = True
                    reason_detail = "Load balancer has no backend pool members"
                elif not has_rules:
                    is_zombie = True
                    reason_detail = "Load balancer has no load balancing rules"

                if is_zombie:
                    resource_group = self._get_resource_group(lb.id)

                    zombie = ZombieResource(
                        id=lb.name,
                        name=lb.name,
                        provider=CloudProvider.AZURE,
                        resource_type=ResourceType.AZURE_LOAD_BALANCER,
                        region=lb.location,
                        reason=ZombieReason.NO_TARGETS if not has_backends else ZombieReason.UNUSED,
                        reason_detail=reason_detail,
                        metadata={
                            "sku": lb.sku.name if lb.sku else None,
                            "resource_group": resource_group,
                            "frontend_count": (
                                len(lb.frontend_ip_configurations)
                                if lb.frontend_ip_configurations
                                else 0
                            ),
                            "backend_pool_count": (
                                len(lb.backend_address_pools) if lb.backend_address_pools else 0
                            ),
                            "rule_count": (
                                len(lb.load_balancing_rules) if lb.load_balancing_rules else 0
                            ),
                            "provisioning_state": lb.provisioning_state,
                            "resource_id": lb.id,
                        },
                    )

                    self.cost_estimator.update_resource_cost(zombie)
                    zombies.append(zombie)

                    self._log.debug(
                        "found_zombie_lb",
                        lb_name=lb.name,
                        reason=zombie.reason.value,
                    )

        except AzureError as e:
            self._log.error("scan_load_balancers_error", region=region, error=str(e))
            raise

        return zombies

    def _scan_snapshots_sync(self, region: str) -> list[ZombieResource]:
        """Scan for old disk snapshots (synchronous)."""
        zombies: list[ZombieResource] = []

        threshold_date = datetime.now(UTC) - timedelta(
            days=self.settings.thresholds.snapshot_age_days
        )

        try:
            for snapshot in self._compute_client.snapshots.list():
                # Filter by region
                if snapshot.location.lower() != region.lower():
                    continue

                # Check snapshot age
                if snapshot.time_created and snapshot.time_created < threshold_date:
                    resource_group = self._get_resource_group(snapshot.id)

                    zombie = ZombieResource(
                        id=snapshot.name,
                        name=snapshot.name,
                        provider=CloudProvider.AZURE,
                        resource_type=ResourceType.AZURE_SNAPSHOT,
                        region=snapshot.location,
                        reason=ZombieReason.AGE_EXCEEDED,
                        reason_detail=(
                            f"Snapshot is older than "
                            f"{self.settings.thresholds.snapshot_age_days} days"
                        ),
                        size_gb=snapshot.disk_size_gb,
                        created_at=snapshot.time_created.replace(tzinfo=None),
                        metadata={
                            "source_resource_id": (
                                snapshot.creation_data.source_resource_id
                                if snapshot.creation_data
                                else None
                            ),
                            "resource_group": resource_group,
                            "provisioning_state": snapshot.provisioning_state,
                            "incremental": (
                                snapshot.incremental if hasattr(snapshot, "incremental") else None
                            ),
                            "resource_id": snapshot.id,
                        },
                    )

                    # Check if source disk still exists
                    if snapshot.creation_data and snapshot.creation_data.source_resource_id:
                        disk_exists = self._check_disk_exists(
                            snapshot.creation_data.source_resource_id
                        )
                        if not disk_exists:
                            zombie.deletion_warning = (
                                "Source disk no longer exists - this may be the only backup"
                            )

                    self.cost_estimator.update_resource_cost(zombie)
                    zombies.append(zombie)

                    self._log.debug(
                        "found_zombie_snapshot",
                        snapshot_name=snapshot.name,
                        age_days=(datetime.now(UTC) - snapshot.time_created).days,
                    )

        except AzureError as e:
            self._log.error("scan_snapshots_error", region=region, error=str(e))
            raise

        return zombies

    def _get_resource_group(self, resource_id: str) -> str:
        """Extract resource group from Azure resource ID."""
        # Format: /subscriptions/{sub}/resourceGroups/{rg}/providers/...
        try:
            parts = resource_id.split("/")
            rg_idx = parts.index("resourceGroups")
            return parts[rg_idx + 1]
        except (ValueError, IndexError):
            return ""

    def _check_disk_exists(self, disk_id: str) -> bool:
        """Check if a disk exists from its resource ID."""
        try:
            resource_group = self._get_resource_group(disk_id)
            disk_name = disk_id.split("/")[-1]
            self._compute_client.disks.get(resource_group, disk_name)
            return True
        except ResourceNotFoundError:
            return False
        except Exception:
            return True  # Assume exists if we can't check

    # -------------------------------------------------------------------------
    # Synchronous delete implementation (wrapped by base class for async)
    # -------------------------------------------------------------------------

    def _delete_resource_sync(self, resource: ZombieResource) -> bool:
        """Delete a zombie resource (synchronous)."""
        try:
            resource_group = resource.metadata.get("resource_group", "")
            if not resource_group:
                self._log.error("missing_resource_group", resource_id=resource.id)
                return False

            match resource.resource_type:
                case ResourceType.AZURE_DISK:
                    return self._delete_disk(resource_group, resource.id)
                case ResourceType.AZURE_PUBLIC_IP:
                    return self._delete_public_ip(resource_group, resource.id)
                case ResourceType.AZURE_LOAD_BALANCER:
                    return self._delete_load_balancer(resource_group, resource.id)
                case ResourceType.AZURE_SNAPSHOT:
                    return self._delete_snapshot(resource_group, resource.id)
                case _:
                    self._log.warning(
                        "unsupported_delete",
                        resource_type=resource.resource_type.value,
                    )
                    return False
        except AzureError as e:
            self._log.error(
                "delete_error",
                resource_id=resource.id,
                error=str(e),
            )
            return False

    def _delete_disk(self, resource_group: str, disk_name: str) -> bool:
        """Delete a managed disk."""
        poller = self._compute_client.disks.begin_delete(resource_group, disk_name)
        poller.wait()
        return True

    def _delete_public_ip(self, resource_group: str, ip_name: str) -> bool:
        """Delete a public IP address."""
        poller = self._network_client.public_ip_addresses.begin_delete(resource_group, ip_name)
        poller.wait()
        return True

    def _delete_load_balancer(self, resource_group: str, lb_name: str) -> bool:
        """Delete a load balancer."""
        poller = self._network_client.load_balancers.begin_delete(resource_group, lb_name)
        poller.wait()
        return True

    def _delete_snapshot(self, resource_group: str, snapshot_name: str) -> bool:
        """Delete a snapshot."""
        poller = self._compute_client.snapshots.begin_delete(resource_group, snapshot_name)
        poller.wait()
        return True

    def get_resource_details(self, resource: ZombieResource) -> dict[str, Any]:
        """Get detailed information about a resource."""
        details: dict[str, Any] = {
            "id": resource.id,
            "name": resource.name,
            "type": resource.resource_type.value,
            "region": resource.region,
            "subscription": self.subscription_id,
            "reason": resource.reason.value,
            "reason_detail": resource.reason_detail,
            "monthly_cost": f"${resource.monthly_cost:.2f}",
            "created_at": resource.created_at.isoformat() if resource.created_at else None,
        }

        if resource.size_gb:
            details["size_gb"] = resource.size_gb

        details.update(resource.metadata)

        return details
