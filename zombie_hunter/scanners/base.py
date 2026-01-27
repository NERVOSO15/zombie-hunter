"""Abstract base scanner interface for cloud providers."""

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

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

    This is an async-first scanner design that enables concurrent scanning
    across multiple regions and resource types for maximum performance.
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
    def _scan_volumes_sync(self, region: str) -> list[ZombieResource]:
        """
        Synchronous scan for zombie volumes (unattached disks).

        Args:
            region: Region to scan

        Returns:
            List of zombie volume resources
        """
        ...

    @abstractmethod
    def _scan_ips_sync(self, region: str) -> list[ZombieResource]:
        """
        Synchronous scan for unused IP addresses.

        Args:
            region: Region to scan

        Returns:
            List of zombie IP resources
        """
        ...

    @abstractmethod
    def _scan_load_balancers_sync(self, region: str) -> list[ZombieResource]:
        """
        Synchronous scan for idle load balancers.

        Args:
            region: Region to scan

        Returns:
            List of zombie load balancer resources
        """
        ...

    @abstractmethod
    def _scan_snapshots_sync(self, region: str) -> list[ZombieResource]:
        """
        Synchronous scan for old/unused snapshots.

        Args:
            region: Region to scan

        Returns:
            List of zombie snapshot resources
        """
        ...

    @abstractmethod
    def _delete_resource_sync(self, resource: ZombieResource) -> bool:
        """
        Synchronous delete of a zombie resource.

        Args:
            resource: The zombie resource to delete

        Returns:
            True if deletion was successful, False otherwise
        """
        ...

    @abstractmethod
    def get_resource_details(self, resource: ZombieResource) -> dict[str, Any]:
        """
        Get detailed information about a resource.

        Args:
            resource: The resource to get details for

        Returns:
            Dictionary with detailed resource information
        """
        ...

    # -------------------------------------------------------------------------
    # Async wrappers - These wrap blocking SDK calls using asyncio.to_thread
    # -------------------------------------------------------------------------

    async def scan_volumes(self, region: str) -> list[ZombieResource]:
        """
        Async scan for zombie volumes.

        Wraps the synchronous SDK calls in asyncio.to_thread for non-blocking execution.
        """
        return await asyncio.to_thread(self._scan_volumes_sync, region)

    async def scan_ips(self, region: str) -> list[ZombieResource]:
        """
        Async scan for unused IP addresses.

        Wraps the synchronous SDK calls in asyncio.to_thread for non-blocking execution.
        """
        return await asyncio.to_thread(self._scan_ips_sync, region)

    async def scan_load_balancers(self, region: str) -> list[ZombieResource]:
        """
        Async scan for idle load balancers.

        Wraps the synchronous SDK calls in asyncio.to_thread for non-blocking execution.
        """
        return await asyncio.to_thread(self._scan_load_balancers_sync, region)

    async def scan_snapshots(self, region: str) -> list[ZombieResource]:
        """
        Async scan for old/unused snapshots.

        Wraps the synchronous SDK calls in asyncio.to_thread for non-blocking execution.
        """
        return await asyncio.to_thread(self._scan_snapshots_sync, region)

    async def delete_resource(self, resource: ZombieResource) -> bool:
        """
        Async delete of a zombie resource.

        Wraps the synchronous SDK calls in asyncio.to_thread for non-blocking execution.
        """
        return await asyncio.to_thread(self._delete_resource_sync, resource)

    # -------------------------------------------------------------------------
    # Main async scanning orchestration
    # -------------------------------------------------------------------------

    async def scan_all(self) -> ScanResult:
        """
        Perform a full async scan across all regions concurrently.

        This method launches scans for all regions simultaneously using
        asyncio.gather(), providing significant performance improvements
        over sequential scanning.

        Returns:
            ScanResult containing all found zombies
        """
        result = ScanResult(
            provider=self.provider,
            regions_scanned=[],
            scan_started_at=datetime.utcnow(),
        )

        self._log.info(
            "starting_async_scan",
            regions=self.regions,
            region_count=len(self.regions),
        )

        # Launch all region scans concurrently
        region_tasks = [self._scan_region(region) for region in self.regions]
        region_results = await asyncio.gather(*region_tasks, return_exceptions=True)

        # Process results
        for region, region_result in zip(self.regions, region_results, strict=True):
            result.regions_scanned.append(region)

            if isinstance(region_result, Exception):
                error_msg = f"Error scanning {region}: {str(region_result)}"
                self._log.error("scan_error", region=region, error=str(region_result))
                result.errors.append(error_msg)
            else:
                result.zombies.extend(region_result)

        result.mark_completed()
        self._log.info(
            "scan_completed",
            zombie_count=result.zombie_count,
            total_savings=result.total_monthly_savings,
            regions_scanned=len(result.regions_scanned),
        )

        return result

    async def _scan_region(self, region: str) -> list[ZombieResource]:
        """
        Scan a single region for all zombie types concurrently.

        Launches scans for all resource types (volumes, IPs, LBs, snapshots)
        in parallel within the region.

        Args:
            region: Region to scan

        Returns:
            List of all zombies found in the region
        """
        self._log.info("scanning_region", region=region)

        # Define scan tasks - all resource types scanned concurrently
        scan_tasks = [
            ("volumes", self.scan_volumes(region)),
            ("ips", self.scan_ips(region)),
            ("load_balancers", self.scan_load_balancers(region)),
            ("snapshots", self.scan_snapshots(region)),
        ]

        # Execute all scans concurrently
        task_names = [name for name, _ in scan_tasks]
        tasks = [task for _, task in scan_tasks]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        zombies: list[ZombieResource] = []

        for resource_type, scan_result in zip(task_names, results, strict=True):
            if isinstance(scan_result, Exception):
                self._log.error(
                    "resource_scan_error",
                    type=resource_type,
                    region=region,
                    error=str(scan_result),
                )
            else:
                zombies.extend(scan_result)
                self._log.debug(
                    "resource_type_scanned",
                    type=resource_type,
                    region=region,
                    count=len(scan_result),
                )

        self._log.info(
            "region_scan_completed",
            region=region,
            zombie_count=len(zombies),
        )

        return zombies

    async def safe_delete(self, resource: ZombieResource) -> tuple[bool, str]:
        """
        Safely delete a resource with dry-run support (async).

        Args:
            resource: The resource to delete

        Returns:
            Tuple of (success, message)
        """
        # DRY_RUN safety check - CRITICAL: Always check before any deletion
        if self.dry_run:
            self._log.info(
                "dry_run_delete",
                resource_id=resource.id,
                resource_type=resource.resource_type.value,
                message="DRY_RUN enabled - deletion skipped",
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
            success = await self.delete_resource(resource)

            if success:
                self._log.info("resource_deleted", resource_id=resource.id)
                return True, f"Successfully deleted {resource.id}"
            else:
                self._log.error("delete_failed", resource_id=resource.id)
                return False, f"Failed to delete {resource.id}"

        except Exception as e:
            self._log.error("delete_error", resource_id=resource.id, error=str(e))
            return False, f"Error deleting {resource.id}: {str(e)}"

    # -------------------------------------------------------------------------
    # Backward compatibility - Synchronous wrappers for non-async contexts
    # -------------------------------------------------------------------------

    def scan_all_sync(self) -> ScanResult:
        """
        Synchronous wrapper for scan_all().

        Use this method when running outside of an async context.
        For best performance, prefer using scan_all() directly in async code.
        """
        return asyncio.run(self.scan_all())


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
