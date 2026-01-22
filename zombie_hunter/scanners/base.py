"""Abstract base scanner interface for cloud providers."""

from abc import ABC, abstractmethod
from datetime import datetime

import structlog

from zombie_hunter.config import Settings
from zombie_hunter.resources.types import (
    CloudProvider,
    ScanResult,
    ZombieResource,
)

logger = structlog.get_logger()


class BaseScanner(ABC):
    """
    Abstract base class for cloud provider scanners.

    Each cloud provider implementation should inherit from this class
    and implement all abstract methods for zombie detection.
    """

    def __init__(self, settings: Settings) -> None:
        """
        Initialize the scanner.

        Args:
            settings: Application settings
        """
        self.settings = settings
        self.dry_run = settings.dry_run
        self._log = logger.bind(provider=self.provider.value)

    @property
    @abstractmethod
    def provider(self) -> CloudProvider:
        """Return the cloud provider this scanner handles."""
        ...

    @property
    @abstractmethod
    def regions(self) -> list[str]:
        """Return the list of regions to scan."""
        ...

    @abstractmethod
    def scan_volumes(self, region: str) -> list[ZombieResource]:
        """
        Scan for zombie volumes (unattached disks).

        Args:
            region: Region to scan

        Returns:
            List of zombie volume resources
        """
        ...

    @abstractmethod
    def scan_ips(self, region: str) -> list[ZombieResource]:
        """
        Scan for unused IP addresses.

        Args:
            region: Region to scan

        Returns:
            List of zombie IP resources
        """
        ...

    @abstractmethod
    def scan_load_balancers(self, region: str) -> list[ZombieResource]:
        """
        Scan for idle load balancers.

        Args:
            region: Region to scan

        Returns:
            List of zombie load balancer resources
        """
        ...

    @abstractmethod
    def scan_snapshots(self, region: str) -> list[ZombieResource]:
        """
        Scan for old/unused snapshots.

        Args:
            region: Region to scan

        Returns:
            List of zombie snapshot resources
        """
        ...

    @abstractmethod
    def delete_resource(self, resource: ZombieResource) -> bool:
        """
        Delete a zombie resource.

        Args:
            resource: The zombie resource to delete

        Returns:
            True if deletion was successful, False otherwise
        """
        ...

    @abstractmethod
    def get_resource_details(self, resource: ZombieResource) -> dict:
        """
        Get detailed information about a resource.

        Args:
            resource: The resource to get details for

        Returns:
            Dictionary with detailed resource information
        """
        ...

    def scan_all(self) -> ScanResult:
        """
        Perform a full scan across all regions.

        Returns:
            ScanResult containing all found zombies
        """
        result = ScanResult(
            provider=self.provider,
            regions_scanned=[],
            scan_started_at=datetime.utcnow(),
        )

        for region in self.regions:
            self._log.info("scanning_region", region=region)
            result.regions_scanned.append(region)

            try:
                # Scan all resource types
                result.zombies.extend(self._scan_region(region))
            except Exception as e:
                error_msg = f"Error scanning {region}: {str(e)}"
                self._log.error("scan_error", region=region, error=str(e))
                result.errors.append(error_msg)

        result.mark_completed()
        self._log.info(
            "scan_completed",
            zombie_count=result.zombie_count,
            total_savings=result.total_monthly_savings,
        )

        return result

    def _scan_region(self, region: str) -> list[ZombieResource]:
        """
        Scan a single region for all zombie types.

        Args:
            region: Region to scan

        Returns:
            List of all zombies found in the region
        """
        zombies: list[ZombieResource] = []

        # Scan each resource type
        scan_methods = [
            ("volumes", self.scan_volumes),
            ("ips", self.scan_ips),
            ("load_balancers", self.scan_load_balancers),
            ("snapshots", self.scan_snapshots),
        ]

        for resource_type, scan_method in scan_methods:
            try:
                self._log.debug("scanning_resource_type", type=resource_type, region=region)
                found = scan_method(region)
                zombies.extend(found)
                self._log.debug(
                    "resource_type_scanned",
                    type=resource_type,
                    region=region,
                    count=len(found),
                )
            except Exception as e:
                self._log.error(
                    "resource_scan_error",
                    type=resource_type,
                    region=region,
                    error=str(e),
                )

        return zombies

    def safe_delete(self, resource: ZombieResource) -> tuple[bool, str]:
        """
        Safely delete a resource with dry-run support.

        Args:
            resource: The resource to delete

        Returns:
            Tuple of (success, message)
        """
        if self.dry_run:
            self._log.info(
                "dry_run_delete",
                resource_id=resource.id,
                resource_type=resource.resource_type.value,
            )
            return True, f"[DRY RUN] Would delete {resource.id}"

        if not resource.can_delete:
            self._log.warning(
                "delete_blocked",
                resource_id=resource.id,
                reason=resource.deletion_warning,
            )
            return False, f"Cannot delete: {resource.deletion_warning}"

        try:
            self._log.info(
                "deleting_resource",
                resource_id=resource.id,
                resource_type=resource.resource_type.value,
            )
            success = self.delete_resource(resource)

            if success:
                self._log.info("resource_deleted", resource_id=resource.id)
                return True, f"Successfully deleted {resource.id}"
            else:
                self._log.error("delete_failed", resource_id=resource.id)
                return False, f"Failed to delete {resource.id}"

        except Exception as e:
            self._log.error("delete_error", resource_id=resource.id, error=str(e))
            return False, f"Error deleting {resource.id}: {str(e)}"


class ScannerRegistry:
    """Registry for cloud provider scanners."""

    _scanners: dict[CloudProvider, type[BaseScanner]] = {}

    @classmethod
    def register(cls, provider: CloudProvider):
        """
        Decorator to register a scanner for a cloud provider.

        Usage:
            @ScannerRegistry.register(CloudProvider.AWS)
            class AWSScanner(BaseScanner):
                ...
        """

        def decorator(scanner_class: type[BaseScanner]):
            cls._scanners[provider] = scanner_class
            return scanner_class

        return decorator

    @classmethod
    def get_scanner(cls, provider: CloudProvider, settings: Settings) -> BaseScanner:
        """
        Get a scanner instance for a cloud provider.

        Args:
            provider: Cloud provider to get scanner for
            settings: Application settings

        Returns:
            Scanner instance

        Raises:
            ValueError: If no scanner is registered for the provider
        """
        if provider not in cls._scanners:
            raise ValueError(f"No scanner registered for provider: {provider.value}")

        scanner_class = cls._scanners[provider]
        return scanner_class(settings)

    @classmethod
    def get_all_scanners(cls, settings: Settings) -> list[BaseScanner]:
        """
        Get scanner instances for all enabled providers.

        Args:
            settings: Application settings

        Returns:
            List of scanner instances for enabled providers
        """
        scanners = []
        for provider in settings.scanner.enabled_providers:
            try:
                scanner = cls.get_scanner(provider, settings)
                scanners.append(scanner)
            except ValueError as e:
                logger.warning("scanner_not_available", provider=provider.value, error=str(e))

        return scanners

    @classmethod
    def registered_providers(cls) -> list[CloudProvider]:
        """Get list of registered providers."""
        return list(cls._scanners.keys())
