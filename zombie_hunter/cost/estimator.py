"""Cost estimation module for zombie resources."""

from zombie_hunter.resources.types import CloudProvider, ResourceType, ZombieResource

# AWS Pricing (approximate, us-east-1 region)
# These are estimates and should be updated based on actual AWS pricing
AWS_PRICING = {
    # EBS Volumes (per GB/month)
    "ebs_gp2_per_gb": 0.10,
    "ebs_gp3_per_gb": 0.08,
    "ebs_io1_per_gb": 0.125,
    "ebs_io2_per_gb": 0.125,
    "ebs_st1_per_gb": 0.045,
    "ebs_sc1_per_gb": 0.015,
    "ebs_standard_per_gb": 0.05,
    # Elastic IPs (per hour when not attached)
    "elastic_ip_hourly": 0.005,  # ~$3.60/month
    # Load Balancers (base cost per hour)
    "alb_hourly": 0.0225,  # ~$16.20/month
    "nlb_hourly": 0.0225,  # ~$16.20/month
    "clb_hourly": 0.025,  # ~$18/month
    # Additional LB costs
    "alb_lcu_hourly": 0.008,  # per LCU-hour
    # Snapshots (per GB/month)
    "ebs_snapshot_per_gb": 0.05,
    "rds_snapshot_per_gb": 0.02,
}

# GCP Pricing (approximate)
GCP_PRICING = {
    # Persistent Disks (per GB/month)
    "pd_standard_per_gb": 0.04,
    "pd_ssd_per_gb": 0.17,
    "pd_balanced_per_gb": 0.10,
    # Static IPs (per hour when not attached)
    "static_ip_hourly": 0.01,  # ~$7.30/month
    # Load Balancers (per hour)
    "lb_forwarding_rule_hourly": 0.025,
    # Snapshots (per GB/month)
    "snapshot_per_gb": 0.026,
}

# Azure Pricing (approximate)
AZURE_PRICING = {
    # Managed Disks (per GB/month)
    "disk_standard_hdd_per_gb": 0.04,
    "disk_standard_ssd_per_gb": 0.075,
    "disk_premium_ssd_per_gb": 0.135,
    # Public IPs (per hour when not attached)
    "public_ip_hourly": 0.005,  # ~$3.60/month
    # Load Balancers (per hour + per rule)
    "lb_hourly": 0.025,
    "lb_rule_hourly": 0.01,
    # Snapshots (per GB/month)
    "snapshot_per_gb": 0.05,
}

HOURS_PER_MONTH = 730  # Average hours in a month


class CostEstimator:
    """Estimates costs for zombie resources."""

    def __init__(
        self,
        aws_pricing: dict[str, float] | None = None,
        gcp_pricing: dict[str, float] | None = None,
        azure_pricing: dict[str, float] | None = None,
    ) -> None:
        """
        Initialize cost estimator with optional custom pricing.

        Args:
            aws_pricing: Custom AWS pricing overrides
            gcp_pricing: Custom GCP pricing overrides
            azure_pricing: Custom Azure pricing overrides
        """
        self.aws_pricing = {**AWS_PRICING, **(aws_pricing or {})}
        self.gcp_pricing = {**GCP_PRICING, **(gcp_pricing or {})}
        self.azure_pricing = {**AZURE_PRICING, **(azure_pricing or {})}

    def estimate_monthly_cost(self, resource: ZombieResource) -> float:
        """
        Estimate monthly cost for a zombie resource.

        Args:
            resource: The zombie resource

        Returns:
            Estimated monthly cost in USD
        """
        if resource.provider == CloudProvider.AWS:
            return self._estimate_aws_cost(resource)
        elif resource.provider == CloudProvider.GCP:
            return self._estimate_gcp_cost(resource)
        elif resource.provider == CloudProvider.AZURE:
            return self._estimate_azure_cost(resource)
        return 0.0

    def _estimate_aws_cost(self, resource: ZombieResource) -> float:
        """Estimate AWS resource cost."""
        size_gb = resource.size_gb or 0
        volume_type = resource.metadata.get("volume_type", "gp3")

        match resource.resource_type:
            case ResourceType.EBS_VOLUME:
                # Get volume type-specific pricing
                price_key = f"ebs_{volume_type}_per_gb"
                price_per_gb = self.aws_pricing.get(price_key, self.aws_pricing["ebs_gp3_per_gb"])
                return size_gb * price_per_gb

            case ResourceType.ELASTIC_IP:
                return self.aws_pricing["elastic_ip_hourly"] * HOURS_PER_MONTH

            case ResourceType.ALB:
                return self.aws_pricing["alb_hourly"] * HOURS_PER_MONTH

            case ResourceType.NLB:
                return self.aws_pricing["nlb_hourly"] * HOURS_PER_MONTH

            case ResourceType.CLB:
                return self.aws_pricing["clb_hourly"] * HOURS_PER_MONTH

            case ResourceType.EBS_SNAPSHOT:
                return size_gb * self.aws_pricing["ebs_snapshot_per_gb"]

            case ResourceType.RDS_SNAPSHOT:
                return size_gb * self.aws_pricing["rds_snapshot_per_gb"]

            case _:
                return 0.0

    def _estimate_gcp_cost(self, resource: ZombieResource) -> float:
        """Estimate GCP resource cost."""
        size_gb = resource.size_gb or 0
        disk_type = resource.metadata.get("disk_type", "pd-standard")

        match resource.resource_type:
            case ResourceType.GCP_DISK:
                # Map disk type to pricing
                type_mapping = {
                    "pd-standard": "pd_standard_per_gb",
                    "pd-ssd": "pd_ssd_per_gb",
                    "pd-balanced": "pd_balanced_per_gb",
                }
                price_key = type_mapping.get(disk_type, "pd_standard_per_gb")
                return size_gb * self.gcp_pricing[price_key]

            case ResourceType.GCP_STATIC_IP:
                return self.gcp_pricing["static_ip_hourly"] * HOURS_PER_MONTH

            case ResourceType.GCP_LOAD_BALANCER:
                return self.gcp_pricing["lb_forwarding_rule_hourly"] * HOURS_PER_MONTH

            case ResourceType.GCP_SNAPSHOT:
                return size_gb * self.gcp_pricing["snapshot_per_gb"]

            case _:
                return 0.0

    def _estimate_azure_cost(self, resource: ZombieResource) -> float:
        """Estimate Azure resource cost."""
        size_gb = resource.size_gb or 0
        disk_type = resource.metadata.get("disk_type", "Standard_HDD")

        match resource.resource_type:
            case ResourceType.AZURE_DISK:
                # Map disk type to pricing
                type_mapping = {
                    "Standard_HDD": "disk_standard_hdd_per_gb",
                    "Standard_SSD": "disk_standard_ssd_per_gb",
                    "Premium_SSD": "disk_premium_ssd_per_gb",
                }
                price_key = type_mapping.get(disk_type, "disk_standard_hdd_per_gb")
                return size_gb * self.azure_pricing[price_key]

            case ResourceType.AZURE_PUBLIC_IP:
                return self.azure_pricing["public_ip_hourly"] * HOURS_PER_MONTH

            case ResourceType.AZURE_LOAD_BALANCER:
                # Base cost + estimated rules
                num_rules = resource.metadata.get("rule_count", 1)
                base_cost = self.azure_pricing["lb_hourly"] * HOURS_PER_MONTH
                rule_cost = self.azure_pricing["lb_rule_hourly"] * HOURS_PER_MONTH * num_rules
                return base_cost + rule_cost

            case ResourceType.AZURE_SNAPSHOT:
                return size_gb * self.azure_pricing["snapshot_per_gb"]

            case _:
                return 0.0

    def update_resource_cost(self, resource: ZombieResource) -> ZombieResource:
        """
        Update the monthly_cost field of a resource.

        Args:
            resource: The zombie resource

        Returns:
            Updated resource with cost estimate
        """
        resource.monthly_cost = self.estimate_monthly_cost(resource)
        return resource

    def get_total_savings(self, resources: list[ZombieResource]) -> float:
        """
        Calculate total potential monthly savings.

        Args:
            resources: List of zombie resources

        Returns:
            Total monthly savings in USD
        """
        return sum(r.monthly_cost for r in resources)

    def get_annual_savings(self, resources: list[ZombieResource]) -> float:
        """
        Calculate total potential annual savings.

        Args:
            resources: List of zombie resources

        Returns:
            Total annual savings in USD
        """
        return self.get_total_savings(resources) * 12

    def format_cost(self, cost: float) -> str:
        """Format cost as currency string."""
        if cost >= 1000:
            return f"${cost:,.0f}"
        return f"${cost:.2f}"

    def get_cost_breakdown(
        self, resources: list[ZombieResource]
    ) -> dict[ResourceType, dict[str, float | int]]:
        """
        Get cost breakdown by resource type.

        Args:
            resources: List of zombie resources

        Returns:
            Dictionary with cost and count per resource type
        """
        breakdown: dict[ResourceType, dict[str, float | int]] = {}

        for resource in resources:
            rt = resource.resource_type
            if rt not in breakdown:
                breakdown[rt] = {"count": 0, "monthly_cost": 0.0}

            breakdown[rt]["count"] = int(breakdown[rt]["count"]) + 1
            breakdown[rt]["monthly_cost"] = (
                float(breakdown[rt]["monthly_cost"]) + resource.monthly_cost
            )

        return breakdown
