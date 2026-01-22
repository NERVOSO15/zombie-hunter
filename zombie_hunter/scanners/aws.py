"""AWS scanner for detecting zombie resources."""

from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError
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


@ScannerRegistry.register(CloudProvider.AWS)
class AWSScanner(BaseScanner):
    """Scanner for AWS zombie resources."""

    def __init__(self, settings: Settings) -> None:
        """Initialize AWS scanner."""
        super().__init__(settings)
        self.cost_estimator = CostEstimator()
        self._clients: dict[str, dict[str, Any]] = {}

    @property
    def provider(self) -> CloudProvider:
        """Return AWS as the cloud provider."""
        return CloudProvider.AWS

    @property
    def regions(self) -> list[str]:
        """Return configured AWS regions."""
        return self.settings.scanner.aws_regions

    def _get_client(self, service: str, region: str) -> Any:
        """Get or create a boto3 client for a service and region."""
        key = f"{service}:{region}"
        if key not in self._clients:
            self._clients[key] = boto3.client(service, region_name=region)
        return self._clients[key]

    def _get_resource_tags(self, tags: list[dict[str, str]] | None) -> dict[str, str]:
        """Convert AWS tag list to dictionary."""
        if not tags:
            return {}
        return {tag["Key"]: tag["Value"] for tag in tags}

    def _get_name_from_tags(self, tags: dict[str, str]) -> str:
        """Extract Name tag value."""
        return tags.get("Name", "")

    def scan_volumes(self, region: str) -> list[ZombieResource]:
        """Scan for unattached EBS volumes."""
        zombies: list[ZombieResource] = []
        ec2 = self._get_client("ec2", region)

        try:
            # Find volumes with state 'available' (not attached)
            paginator = ec2.get_paginator("describe_volumes")
            for page in paginator.paginate(
                Filters=[{"Name": "status", "Values": ["available"]}]
            ):
                for volume in page["Volumes"]:
                    tags = self._get_resource_tags(volume.get("Tags"))

                    zombie = ZombieResource(
                        id=volume["VolumeId"],
                        name=self._get_name_from_tags(tags),
                        provider=CloudProvider.AWS,
                        resource_type=ResourceType.EBS_VOLUME,
                        region=region,
                        reason=ZombieReason.UNATTACHED,
                        reason_detail="Volume is not attached to any instance",
                        size_gb=volume["Size"],
                        created_at=volume["CreateTime"].replace(tzinfo=None),
                        tags=tags,
                        metadata={
                            "volume_type": volume["VolumeType"],
                            "iops": volume.get("Iops"),
                            "throughput": volume.get("Throughput"),
                            "encrypted": volume.get("Encrypted", False),
                            "snapshot_id": volume.get("SnapshotId", ""),
                        },
                    )

                    # Estimate cost
                    self.cost_estimator.update_resource_cost(zombie)
                    zombies.append(zombie)

                    self._log.debug(
                        "found_zombie_volume",
                        volume_id=volume["VolumeId"],
                        size_gb=volume["Size"],
                        monthly_cost=zombie.monthly_cost,
                    )

        except ClientError as e:
            self._log.error("scan_volumes_error", region=region, error=str(e))
            raise

        return zombies

    def scan_ips(self, region: str) -> list[ZombieResource]:
        """Scan for unattached Elastic IPs."""
        zombies: list[ZombieResource] = []
        ec2 = self._get_client("ec2", region)

        try:
            response = ec2.describe_addresses()

            for address in response["Addresses"]:
                # An EIP is unattached if it has no AssociationId
                if "AssociationId" not in address:
                    tags = self._get_resource_tags(address.get("Tags"))

                    zombie = ZombieResource(
                        id=address.get("AllocationId", address.get("PublicIp", "")),
                        name=self._get_name_from_tags(tags),
                        provider=CloudProvider.AWS,
                        resource_type=ResourceType.ELASTIC_IP,
                        region=region,
                        reason=ZombieReason.UNATTACHED,
                        reason_detail="Elastic IP is not associated with any resource",
                        tags=tags,
                        metadata={
                            "public_ip": address.get("PublicIp"),
                            "domain": address.get("Domain"),
                            "network_border_group": address.get("NetworkBorderGroup"),
                        },
                    )

                    self.cost_estimator.update_resource_cost(zombie)
                    zombies.append(zombie)

                    self._log.debug(
                        "found_zombie_eip",
                        allocation_id=address.get("AllocationId"),
                        public_ip=address.get("PublicIp"),
                    )

        except ClientError as e:
            self._log.error("scan_ips_error", region=region, error=str(e))
            raise

        return zombies

    def scan_load_balancers(self, region: str) -> list[ZombieResource]:
        """Scan for idle load balancers (ALB/NLB)."""
        zombies: list[ZombieResource] = []
        elbv2 = self._get_client("elbv2", region)
        cloudwatch = self._get_client("cloudwatch", region)

        try:
            paginator = elbv2.get_paginator("describe_load_balancers")

            for page in paginator.paginate():
                for lb in page["LoadBalancers"]:
                    lb_arn = lb["LoadBalancerArn"]
                    lb_name = lb["LoadBalancerName"]
                    lb_type = lb["Type"]

                    # Check if LB has any target groups with targets
                    has_targets = self._lb_has_targets(elbv2, lb_arn)

                    # Check CloudWatch for traffic
                    has_traffic = self._lb_has_traffic(cloudwatch, lb_arn, lb_type)

                    # Determine zombie status
                    is_zombie = False
                    reason_detail = ""

                    if not has_targets:
                        is_zombie = True
                        reason_detail = "Load balancer has no registered targets"
                    elif not has_traffic:
                        is_zombie = True
                        reason_detail = (
                            f"No traffic in the last {self.settings.thresholds.lb_idle_days} days"
                        )

                    if is_zombie:
                        # Determine resource type
                        resource_type = (
                            ResourceType.ALB if lb_type == "application" else ResourceType.NLB
                        )

                        zombie = ZombieResource(
                            id=lb_arn,
                            name=lb_name,
                            provider=CloudProvider.AWS,
                            resource_type=resource_type,
                            region=region,
                            reason=ZombieReason.NO_TARGETS if not has_targets else ZombieReason.NO_TRAFFIC,
                            reason_detail=reason_detail,
                            created_at=lb["CreatedTime"].replace(tzinfo=None),
                            metadata={
                                "dns_name": lb.get("DNSName"),
                                "scheme": lb.get("Scheme"),
                                "vpc_id": lb.get("VpcId"),
                                "type": lb_type,
                                "has_targets": has_targets,
                                "has_traffic": has_traffic,
                            },
                        )

                        self.cost_estimator.update_resource_cost(zombie)
                        zombies.append(zombie)

                        self._log.debug(
                            "found_zombie_lb",
                            lb_name=lb_name,
                            reason=zombie.reason.value,
                        )

        except ClientError as e:
            self._log.error("scan_load_balancers_error", region=region, error=str(e))
            raise

        return zombies

    def _lb_has_targets(self, elbv2: Any, lb_arn: str) -> bool:
        """Check if load balancer has any registered targets."""
        try:
            # Get target groups for this LB
            tg_response = elbv2.describe_target_groups(LoadBalancerArn=lb_arn)

            for tg in tg_response["TargetGroups"]:
                # Check each target group for registered targets
                health_response = elbv2.describe_target_health(
                    TargetGroupArn=tg["TargetGroupArn"]
                )
                if health_response["TargetHealthDescriptions"]:
                    return True

            return False

        except ClientError:
            # If we can't check, assume it has targets (safer)
            return True

    def _lb_has_traffic(self, cloudwatch: Any, lb_arn: str, lb_type: str) -> bool:
        """Check if load balancer has recent traffic via CloudWatch."""
        try:
            # Extract LB name from ARN for CloudWatch dimension
            # ARN format: arn:aws:elasticloadbalancing:region:account:loadbalancer/type/name/id
            lb_dimension = "/".join(lb_arn.split("/")[-3:])

            metric_name = (
                "RequestCount" if lb_type == "application" else "ProcessedBytes"
            )

            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(days=self.settings.thresholds.lb_idle_days)

            response = cloudwatch.get_metric_statistics(
                Namespace="AWS/ApplicationELB" if lb_type == "application" else "AWS/NetworkELB",
                MetricName=metric_name,
                Dimensions=[{"Name": "LoadBalancer", "Value": lb_dimension}],
                StartTime=start_time,
                EndTime=end_time,
                Period=86400,  # 1 day
                Statistics=["Sum"],
            )

            # Check if there's any traffic
            for datapoint in response.get("Datapoints", []):
                if datapoint.get("Sum", 0) > 0:
                    return True

            return False

        except ClientError:
            # If we can't check, assume it has traffic (safer)
            return True

    def scan_snapshots(self, region: str) -> list[ZombieResource]:
        """Scan for old RDS snapshots."""
        zombies: list[ZombieResource] = []
        rds = self._get_client("rds", region)

        threshold_date = datetime.now(timezone.utc) - timedelta(
            days=self.settings.thresholds.snapshot_age_days
        )

        try:
            # Scan manual RDS snapshots (not automated backups)
            paginator = rds.get_paginator("describe_db_snapshots")

            for page in paginator.paginate(SnapshotType="manual"):
                for snapshot in page["DBSnapshots"]:
                    # Check if snapshot is older than threshold
                    snapshot_time = snapshot["SnapshotCreateTime"]
                    if snapshot_time.replace(tzinfo=timezone.utc) < threshold_date:
                        zombie = ZombieResource(
                            id=snapshot["DBSnapshotIdentifier"],
                            name=snapshot["DBSnapshotIdentifier"],
                            provider=CloudProvider.AWS,
                            resource_type=ResourceType.RDS_SNAPSHOT,
                            region=region,
                            reason=ZombieReason.AGE_EXCEEDED,
                            reason_detail=f"Snapshot is older than {self.settings.thresholds.snapshot_age_days} days",
                            size_gb=snapshot.get("AllocatedStorage", 0),
                            created_at=snapshot_time.replace(tzinfo=None),
                            metadata={
                                "db_instance_identifier": snapshot.get("DBInstanceIdentifier"),
                                "engine": snapshot.get("Engine"),
                                "engine_version": snapshot.get("EngineVersion"),
                                "status": snapshot.get("Status"),
                                "encrypted": snapshot.get("Encrypted", False),
                            },
                        )

                        # Check if source DB still exists
                        db_exists = self._check_db_exists(
                            rds, snapshot.get("DBInstanceIdentifier")
                        )
                        if not db_exists:
                            zombie.deletion_warning = (
                                "Source database no longer exists - "
                                "this may be the only backup"
                            )

                        self.cost_estimator.update_resource_cost(zombie)
                        zombies.append(zombie)

                        self._log.debug(
                            "found_zombie_snapshot",
                            snapshot_id=snapshot["DBSnapshotIdentifier"],
                            age_days=(datetime.now(timezone.utc) - snapshot_time).days,
                        )

        except ClientError as e:
            self._log.error("scan_snapshots_error", region=region, error=str(e))
            raise

        return zombies

    def _check_db_exists(self, rds: Any, db_identifier: str | None) -> bool:
        """Check if an RDS instance exists."""
        if not db_identifier:
            return False

        try:
            rds.describe_db_instances(DBInstanceIdentifier=db_identifier)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "DBInstanceNotFound":
                return False
            return True  # Assume exists if we can't check

    def delete_resource(self, resource: ZombieResource) -> bool:
        """Delete a zombie resource."""
        try:
            match resource.resource_type:
                case ResourceType.EBS_VOLUME:
                    return self._delete_volume(resource)
                case ResourceType.ELASTIC_IP:
                    return self._delete_eip(resource)
                case ResourceType.ALB | ResourceType.NLB:
                    return self._delete_load_balancer(resource)
                case ResourceType.RDS_SNAPSHOT:
                    return self._delete_rds_snapshot(resource)
                case _:
                    self._log.warning(
                        "unsupported_delete",
                        resource_type=resource.resource_type.value,
                    )
                    return False
        except ClientError as e:
            self._log.error(
                "delete_error",
                resource_id=resource.id,
                error=str(e),
            )
            return False

    def _delete_volume(self, resource: ZombieResource) -> bool:
        """Delete an EBS volume."""
        ec2 = self._get_client("ec2", resource.region)
        ec2.delete_volume(VolumeId=resource.id)
        return True

    def _delete_eip(self, resource: ZombieResource) -> bool:
        """Release an Elastic IP."""
        ec2 = self._get_client("ec2", resource.region)
        ec2.release_address(AllocationId=resource.id)
        return True

    def _delete_load_balancer(self, resource: ZombieResource) -> bool:
        """Delete a load balancer."""
        elbv2 = self._get_client("elbv2", resource.region)
        elbv2.delete_load_balancer(LoadBalancerArn=resource.id)
        return True

    def _delete_rds_snapshot(self, resource: ZombieResource) -> bool:
        """Delete an RDS snapshot."""
        rds = self._get_client("rds", resource.region)
        rds.delete_db_snapshot(DBSnapshotIdentifier=resource.id)
        return True

    def get_resource_details(self, resource: ZombieResource) -> dict:
        """Get detailed information about a resource."""
        details = {
            "id": resource.id,
            "name": resource.name,
            "type": resource.resource_type.value,
            "region": resource.region,
            "reason": resource.reason.value,
            "reason_detail": resource.reason_detail,
            "monthly_cost": f"${resource.monthly_cost:.2f}",
            "created_at": resource.created_at.isoformat() if resource.created_at else None,
            "tags": resource.tags,
        }

        # Add type-specific details
        if resource.size_gb:
            details["size_gb"] = resource.size_gb

        details.update(resource.metadata)

        return details
