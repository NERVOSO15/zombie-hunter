"""GCP scanner for detecting zombie resources."""

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

try:
    from google.api_core.exceptions import GoogleAPICallError, NotFound
    from google.cloud import compute_v1

    GCP_AVAILABLE = True
except ImportError:
    GCP_AVAILABLE = False

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


@ScannerRegistry.register(CloudProvider.GCP)
class GCPScanner(BaseScanner):
    """Scanner for GCP zombie resources."""

    def __init__(self, settings: Settings) -> None:
        """Initialize GCP scanner."""
        if not GCP_AVAILABLE:
            raise ImportError(
                "GCP SDK not installed. Install with: "
                "pip install google-cloud-compute google-cloud-monitoring"
            )

        super().__init__(settings)
        self.cost_estimator = CostEstimator()
        self.project_id = settings.gcp.project_id

        if not self.project_id:
            raise ValueError("GCP project ID is required. Set GCP_PROJECT_ID environment variable.")

        # Initialize clients
        self._disk_client = compute_v1.DisksClient()
        self._address_client = compute_v1.AddressesClient()
        self._forwarding_rule_client = compute_v1.ForwardingRulesClient()
        self._snapshot_client = compute_v1.SnapshotsClient()
        self._instance_client = compute_v1.InstancesClient()

    @property
    def provider(self) -> CloudProvider:
        """Return GCP as the cloud provider."""
        return CloudProvider.GCP

    @property
    def regions(self) -> list[str]:
        """Return configured GCP regions."""
        return self.settings.scanner.gcp_regions

    def scan_volumes(self, region: str) -> list[ZombieResource]:
        """Scan for unattached persistent disks in a region."""
        zombies: list[ZombieResource] = []

        try:
            # List all disks in the zone (GCP uses zones, not regions for disks)
            # We'll check zones within the region
            zones = self._get_zones_for_region(region)

            for zone in zones:
                try:
                    request = compute_v1.ListDisksRequest(
                        project=self.project_id,
                        zone=zone,
                    )

                    for disk in self._disk_client.list(request=request):
                        # Check if disk is not attached to any instance
                        if not disk.users:  # Empty users list means not attached
                            # Parse creation timestamp
                            created_at = None
                            if disk.creation_timestamp:
                                created_at = datetime.fromisoformat(
                                    disk.creation_timestamp.replace("Z", "+00:00")
                                ).replace(tzinfo=None)

                            # Get disk type name from URL
                            disk_type = "pd-standard"
                            if disk.type_:
                                disk_type = disk.type_.split("/")[-1]

                            zombie = ZombieResource(
                                id=disk.name,
                                name=disk.name,
                                provider=CloudProvider.GCP,
                                resource_type=ResourceType.GCP_DISK,
                                region=zone,
                                reason=ZombieReason.UNATTACHED,
                                reason_detail="Disk is not attached to any instance",
                                size_gb=disk.size_gb,
                                created_at=created_at,
                                metadata={
                                    "disk_type": disk_type,
                                    "status": disk.status,
                                    "self_link": disk.self_link,
                                    "source_image": disk.source_image or "",
                                    "source_snapshot": disk.source_snapshot or "",
                                },
                            )

                            self.cost_estimator.update_resource_cost(zombie)
                            zombies.append(zombie)

                            self._log.debug(
                                "found_zombie_disk",
                                disk_name=disk.name,
                                zone=zone,
                                size_gb=disk.size_gb,
                            )

                except GoogleAPICallError as e:
                    self._log.error("scan_zone_error", zone=zone, error=str(e))

        except Exception as e:
            self._log.error("scan_volumes_error", region=region, error=str(e))
            raise

        return zombies

    def scan_ips(self, region: str) -> list[ZombieResource]:
        """Scan for unused static IP addresses."""
        zombies: list[ZombieResource] = []

        try:
            request = compute_v1.ListAddressesRequest(
                project=self.project_id,
                region=region,
            )

            for address in self._address_client.list(request=request):
                # Check if address is RESERVED but not IN_USE
                if address.status == "RESERVED":
                    created_at = None
                    if address.creation_timestamp:
                        created_at = datetime.fromisoformat(
                            address.creation_timestamp.replace("Z", "+00:00")
                        ).replace(tzinfo=None)

                    zombie = ZombieResource(
                        id=address.name,
                        name=address.name,
                        provider=CloudProvider.GCP,
                        resource_type=ResourceType.GCP_STATIC_IP,
                        region=region,
                        reason=ZombieReason.UNATTACHED,
                        reason_detail="Static IP is reserved but not in use",
                        created_at=created_at,
                        metadata={
                            "address": address.address,
                            "address_type": address.address_type,
                            "network_tier": address.network_tier,
                            "self_link": address.self_link,
                        },
                    )

                    self.cost_estimator.update_resource_cost(zombie)
                    zombies.append(zombie)

                    self._log.debug(
                        "found_zombie_ip",
                        ip_name=address.name,
                        address=address.address,
                    )

        except GoogleAPICallError as e:
            self._log.error("scan_ips_error", region=region, error=str(e))
            raise

        return zombies

    def scan_load_balancers(self, region: str) -> list[ZombieResource]:
        """Scan for unused forwarding rules (load balancer frontend)."""
        zombies: list[ZombieResource] = []

        try:
            request = compute_v1.ListForwardingRulesRequest(
                project=self.project_id,
                region=region,
            )

            for rule in self._forwarding_rule_client.list(request=request):
                # Check if forwarding rule has no backend service or target
                has_backend = bool(rule.backend_service or rule.target)

                if not has_backend:
                    created_at = None
                    if rule.creation_timestamp:
                        created_at = datetime.fromisoformat(
                            rule.creation_timestamp.replace("Z", "+00:00")
                        ).replace(tzinfo=None)

                    zombie = ZombieResource(
                        id=rule.name,
                        name=rule.name,
                        provider=CloudProvider.GCP,
                        resource_type=ResourceType.GCP_LOAD_BALANCER,
                        region=region,
                        reason=ZombieReason.NO_TARGETS,
                        reason_detail="Forwarding rule has no backend service configured",
                        created_at=created_at,
                        metadata={
                            "ip_address": rule.I_p_address,
                            "ip_protocol": rule.I_p_protocol,
                            "port_range": rule.port_range,
                            "load_balancing_scheme": rule.load_balancing_scheme,
                            "self_link": rule.self_link,
                        },
                    )

                    self.cost_estimator.update_resource_cost(zombie)
                    zombies.append(zombie)

                    self._log.debug(
                        "found_zombie_lb",
                        rule_name=rule.name,
                        region=region,
                    )

        except GoogleAPICallError as e:
            self._log.error("scan_load_balancers_error", region=region, error=str(e))
            raise

        return zombies

    def scan_snapshots(self, region: str) -> list[ZombieResource]:
        """Scan for old snapshots. Note: GCP snapshots are global, not regional."""
        zombies: list[ZombieResource] = []

        # Only scan once (snapshots are global)
        if region != self.regions[0]:
            return zombies

        threshold_date = datetime.now(UTC) - timedelta(
            days=self.settings.thresholds.snapshot_age_days
        )

        try:
            request = compute_v1.ListSnapshotsRequest(
                project=self.project_id,
            )

            for snapshot in self._snapshot_client.list(request=request):
                # Parse creation timestamp
                created_at = None
                if snapshot.creation_timestamp:
                    created_at = datetime.fromisoformat(
                        snapshot.creation_timestamp.replace("Z", "+00:00")
                    )

                # Check if snapshot is older than threshold
                if created_at and created_at < threshold_date:
                    zombie = ZombieResource(
                        id=snapshot.name,
                        name=snapshot.name,
                        provider=CloudProvider.GCP,
                        resource_type=ResourceType.GCP_SNAPSHOT,
                        region="global",
                        reason=ZombieReason.AGE_EXCEEDED,
                        reason_detail=(
                            f"Snapshot is older than "
                            f"{self.settings.thresholds.snapshot_age_days} days"
                        ),
                        size_gb=snapshot.disk_size_gb,
                        created_at=created_at.replace(tzinfo=None),
                        metadata={
                            "source_disk": snapshot.source_disk,
                            "status": snapshot.status,
                            "storage_bytes": snapshot.storage_bytes,
                            "self_link": snapshot.self_link,
                        },
                    )

                    # Check if source disk still exists
                    disk_exists = self._check_disk_exists(snapshot.source_disk)
                    if not disk_exists:
                        zombie.deletion_warning = (
                            "Source disk no longer exists - this may be the only backup"
                        )

                    self.cost_estimator.update_resource_cost(zombie)
                    zombies.append(zombie)

                    self._log.debug(
                        "found_zombie_snapshot",
                        snapshot_name=snapshot.name,
                        age_days=(datetime.now(UTC) - created_at).days,
                    )

        except GoogleAPICallError as e:
            self._log.error("scan_snapshots_error", error=str(e))
            raise

        return zombies

    def _get_zones_for_region(self, region: str) -> list[str]:
        """Get zones for a given region."""
        # Common zone suffixes
        suffixes = ["a", "b", "c", "f"]
        return [f"{region}-{suffix}" for suffix in suffixes]

    def _check_disk_exists(self, disk_self_link: str | None) -> bool:
        """Check if a disk exists from its self link."""
        if not disk_self_link:
            return False

        try:
            # Parse zone and disk name from self link
            # Format: .../zones/ZONE/disks/DISK_NAME
            parts = disk_self_link.split("/")
            if "zones" in parts:
                zone_idx = parts.index("zones")
                zone = parts[zone_idx + 1]
                disk_name = parts[-1]

                request = compute_v1.GetDiskRequest(
                    project=self.project_id,
                    zone=zone,
                    disk=disk_name,
                )
                self._disk_client.get(request=request)
                return True
        except NotFound:
            return False
        except Exception:
            return True  # Assume exists if we can't check

        return False

    def delete_resource(self, resource: ZombieResource) -> bool:
        """Delete a zombie resource."""
        try:
            match resource.resource_type:
                case ResourceType.GCP_DISK:
                    return self._delete_disk(resource)
                case ResourceType.GCP_STATIC_IP:
                    return self._delete_address(resource)
                case ResourceType.GCP_LOAD_BALANCER:
                    return self._delete_forwarding_rule(resource)
                case ResourceType.GCP_SNAPSHOT:
                    return self._delete_snapshot(resource)
                case _:
                    self._log.warning(
                        "unsupported_delete",
                        resource_type=resource.resource_type.value,
                    )
                    return False
        except GoogleAPICallError as e:
            self._log.error(
                "delete_error",
                resource_id=resource.id,
                error=str(e),
            )
            return False

    def _delete_disk(self, resource: ZombieResource) -> bool:
        """Delete a persistent disk."""
        request = compute_v1.DeleteDiskRequest(
            project=self.project_id,
            zone=resource.region,  # Zone is stored in region for disks
            disk=resource.id,
        )
        operation = self._disk_client.delete(request=request)
        return self._wait_for_operation(operation, resource.region)

    def _delete_address(self, resource: ZombieResource) -> bool:
        """Release a static IP address."""
        request = compute_v1.DeleteAddressRequest(
            project=self.project_id,
            region=resource.region,
            address=resource.id,
        )
        operation = self._address_client.delete(request=request)
        return self._wait_for_regional_operation(operation, resource.region)

    def _delete_forwarding_rule(self, resource: ZombieResource) -> bool:
        """Delete a forwarding rule."""
        request = compute_v1.DeleteForwardingRuleRequest(
            project=self.project_id,
            region=resource.region,
            forwarding_rule=resource.id,
        )
        operation = self._forwarding_rule_client.delete(request=request)
        return self._wait_for_regional_operation(operation, resource.region)

    def _delete_snapshot(self, resource: ZombieResource) -> bool:
        """Delete a snapshot."""
        request = compute_v1.DeleteSnapshotRequest(
            project=self.project_id,
            snapshot=resource.id,
        )
        operation = self._snapshot_client.delete(request=request)
        return self._wait_for_global_operation(operation)

    def _wait_for_operation(self, operation: Any, zone: str, timeout: int = 300) -> bool:
        """Wait for a zonal operation to complete."""
        try:
            operations_client = compute_v1.ZoneOperationsClient()
            while operation.status != compute_v1.Operation.Status.DONE:
                operation = operations_client.get(
                    project=self.project_id,
                    zone=zone,
                    operation=operation.name,
                )
            return operation.error is None
        except Exception as e:
            self._log.error("operation_wait_error", error=str(e))
            return False

    def _wait_for_regional_operation(
        self, operation: Any, region: str, timeout: int = 300
    ) -> bool:
        """Wait for a regional operation to complete."""
        try:
            operations_client = compute_v1.RegionOperationsClient()
            while operation.status != compute_v1.Operation.Status.DONE:
                operation = operations_client.get(
                    project=self.project_id,
                    region=region,
                    operation=operation.name,
                )
            return operation.error is None
        except Exception as e:
            self._log.error("operation_wait_error", error=str(e))
            return False

    def _wait_for_global_operation(self, operation: Any, timeout: int = 300) -> bool:
        """Wait for a global operation to complete."""
        try:
            operations_client = compute_v1.GlobalOperationsClient()
            while operation.status != compute_v1.Operation.Status.DONE:
                operation = operations_client.get(
                    project=self.project_id,
                    operation=operation.name,
                )
            return operation.error is None
        except Exception as e:
            self._log.error("operation_wait_error", error=str(e))
            return False

    def get_resource_details(self, resource: ZombieResource) -> dict:
        """Get detailed information about a resource."""
        details = {
            "id": resource.id,
            "name": resource.name,
            "type": resource.resource_type.value,
            "region": resource.region,
            "project": self.project_id,
            "reason": resource.reason.value,
            "reason_detail": resource.reason_detail,
            "monthly_cost": f"${resource.monthly_cost:.2f}",
            "created_at": resource.created_at.isoformat() if resource.created_at else None,
        }

        if resource.size_gb:
            details["size_gb"] = resource.size_gb

        details.update(resource.metadata)

        return details
