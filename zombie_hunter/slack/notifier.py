"""Slack notification module for zombie resource alerts."""

import json
from typing import Any

import structlog
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from zombie_hunter.config import Settings, SlackMode
from zombie_hunter.cost.estimator import CostEstimator
from zombie_hunter.resources.types import (
    AggregatedScanResult,
    ResourceType,
    ZombieResource,
)

logger = structlog.get_logger()


class SlackNotifier:
    """Sends zombie resource notifications to Slack."""

    def __init__(self, settings: Settings) -> None:
        """
        Initialize Slack notifier.

        Args:
            settings: Application settings
        """
        self.settings = settings
        self.slack_settings = settings.slack
        self.client = WebClient(token=self.slack_settings.bot_token)
        self.cost_estimator = CostEstimator()
        self._log = logger.bind(component="slack_notifier")

    def send_scan_results(self, results: AggregatedScanResult) -> bool:
        """
        Send scan results to Slack.

        Args:
            results: Aggregated scan results

        Returns:
            True if notification was sent successfully
        """
        if not results.all_zombies:
            self._log.info("no_zombies_found", message="No zombies to report")
            return self._send_no_zombies_message()

        try:
            # Always send summary
            self._send_summary_message(results)

            # Send individual resource notifications if configured
            if (
                self.slack_settings.mode == SlackMode.INTERACTIVE
                and self.slack_settings.post_individual_resources
            ):
                zombies_to_post = results.all_zombies[: self.slack_settings.max_individual_posts]
                for zombie in zombies_to_post:
                    self._send_zombie_notification(zombie)

                remaining = len(results.all_zombies) - len(zombies_to_post)
                if remaining > 0:
                    self._send_remaining_count_message(remaining)

            return True

        except SlackApiError as e:
            self._log.error("slack_api_error", error=str(e))
            return False

    def _send_no_zombies_message(self) -> bool:
        """Send a message when no zombies are found."""
        try:
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            ":white_check_mark: *Zombie Hunter Scan Complete*\n\n"
                            "No zombie resources found! Your cloud is clean."
                        ),
                    },
                },
            ]

            self.client.chat_postMessage(
                channel=self.slack_settings.channel,
                blocks=blocks,
                text="No zombie resources found!",
            )
            return True

        except SlackApiError as e:
            self._log.error("slack_send_error", error=str(e))
            return False

    def _send_summary_message(self, results: AggregatedScanResult) -> None:
        """Send summary message with scan results using enhanced Block Kit."""
        # Build breakdown text
        breakdown_lines = []
        all_zombies = results.all_zombies
        breakdown = self.cost_estimator.get_cost_breakdown(all_zombies)

        for resource_type, data in sorted(breakdown.items(), key=lambda x: -x[1]["monthly_cost"]):
            type_name = resource_type.value.replace("_", " ").title()
            count = data["count"]
            cost = data["monthly_cost"]
            breakdown_lines.append(f"• {type_name}: {count} (${cost:.2f}/mo)")

        breakdown_text = "\n".join(breakdown_lines) if breakdown_lines else "No zombies found"

        # Calculate annual savings
        annual_savings = results.total_monthly_savings * 12

        # Determine savings tier for visual emphasis
        if annual_savings >= 10000:
            savings_emoji = "🔥"
            savings_note = "Critical - Take action now!"
        elif annual_savings >= 1000:
            savings_emoji = "⚠️"
            savings_note = "Significant savings available"
        else:
            savings_emoji = "💰"
            savings_note = "Cleanup recommended"

        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🧟 Zombie Hunter Scan Report",
                    "emoji": True,
                },
            },
            # Prominent Savings Banner
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{savings_emoji} *Total Potential Savings: "
                        f"${results.total_monthly_savings:.2f}/month "
                        f"(${annual_savings:,.2f}/year)*\n"
                        f"_{savings_note}_"
                    ),
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*🧟 Total Zombies:*\n{results.total_zombie_count}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"*☁️ Providers:*\n"
                            f"{', '.join(p.value.upper() for p in results.providers_scanned)}"
                        ),
                    },
                ],
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*📊 Breakdown by Resource Type:*\n{breakdown_text}",
                },
            },
        ]

        # Add scan metadata
        if results.results:
            regions = []
            for r in results.results:
                regions.extend(r.regions_scanned)
            regions_text = ", ".join(sorted(set(regions)))

            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"Scan ID: `{results.scan_id}` | Regions: {regions_text}",
                        }
                    ],
                }
            )

        self.client.chat_postMessage(
            channel=self.slack_settings.channel,
            blocks=blocks,
            text=(
                f"Zombie Hunter found {results.total_zombie_count} zombies "
                f"(${results.total_monthly_savings:.2f}/mo potential savings)"
            ),
        )

    def _send_zombie_notification(self, zombie: ZombieResource) -> None:
        """Send individual zombie notification with action buttons."""
        blocks = self._build_zombie_blocks(zombie)

        self.client.chat_postMessage(
            channel=self.slack_settings.channel,
            blocks=blocks,
            text=f"Zombie found: {zombie.resource_type.value} - {zombie.id}",
        )

    def _build_zombie_blocks(self, zombie: ZombieResource) -> list[dict[str, Any]]:
        """Build Slack blocks for a zombie resource notification."""
        # Resource type emoji mapping
        type_emoji = {
            ResourceType.EBS_VOLUME: "💾",
            ResourceType.ELASTIC_IP: "🌐",
            ResourceType.ALB: "⚖️",
            ResourceType.NLB: "⚖️",
            ResourceType.RDS_SNAPSHOT: "📸",
            ResourceType.GCP_DISK: "💾",
            ResourceType.GCP_STATIC_IP: "🌐",
            ResourceType.AZURE_DISK: "💾",
            ResourceType.AZURE_PUBLIC_IP: "🌐",
        }

        emoji = type_emoji.get(zombie.resource_type, "🧟")
        type_name = zombie.resource_type.value.replace("_", " ").title()

        # Build info fields
        fields = [
            {"type": "mrkdwn", "text": f"*Type:*\n{type_name}"},
            {"type": "mrkdwn", "text": f"*ID:*\n`{zombie.id}`"},
            {"type": "mrkdwn", "text": f"*Region:*\n{zombie.region}"},
            {"type": "mrkdwn", "text": f"*Monthly Cost:*\n${zombie.monthly_cost:.2f}"},
        ]

        if zombie.size_gb:
            fields.append({"type": "mrkdwn", "text": f"*Size:*\n{zombie.size_gb} GB"})

        if zombie.age_days is not None:
            fields.append({"type": "mrkdwn", "text": f"*Age:*\n{zombie.age_days} days"})

        blocks: list[dict[str, Any]] = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{emoji} *Zombie Resource Found*",
                },
            },
            {
                "type": "section",
                "fields": fields[:6],  # Slack limit: 10 fields max, but 6 looks better
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Reason:* {zombie.reason_detail}",
                },
            },
        ]

        # Add warning if present
        if zombie.deletion_warning:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"⚠️ *Warning:* {zombie.deletion_warning}",
                    },
                }
            )

        # Add tags if present
        if zombie.tags:
            tags_text = ", ".join(f"`{k}={v}`" for k, v in list(zombie.tags.items())[:5])
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"Tags: {tags_text}"}],
                }
            )

        # Add action buttons for interactive mode
        if self.slack_settings.mode == SlackMode.INTERACTIVE:
            action_value = json.dumps(
                {
                    "resource_id": zombie.id,
                    "resource_type": zombie.resource_type.value,
                    "provider": zombie.provider.value,
                    "region": zombie.region,
                }
            )

            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✅ Delete", "emoji": True},
                            "style": "danger",
                            "action_id": "delete_zombie",
                            "value": action_value,
                            "confirm": {
                                "title": {"type": "plain_text", "text": "Confirm Deletion"},
                                "text": {
                                    "type": "mrkdwn",
                                    "text": (
                                        f"Are you sure you want to delete `{zombie.id}`?\n\n"
                                        "This action cannot be undone."
                                    ),
                                },
                                "confirm": {"type": "plain_text", "text": "Delete"},
                                "deny": {"type": "plain_text", "text": "Cancel"},
                            },
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "❌ Ignore", "emoji": True},
                            "action_id": "ignore_zombie",
                            "value": action_value,
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "📋 Details", "emoji": True},
                            "action_id": "view_details",
                            "value": action_value,
                        },
                    ],
                }
            )

        return blocks

    def _send_remaining_count_message(self, remaining: int) -> None:
        """Send message about remaining zombies not posted individually."""
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"_...and {remaining} more zombie resources. "
                        "Check the summary above for full breakdown._"
                    ),
                },
            },
        ]

        self.client.chat_postMessage(
            channel=self.slack_settings.channel,
            blocks=blocks,
            text=f"...and {remaining} more zombie resources",
        )

    def send_deletion_result(
        self,
        resource_id: str,
        success: bool,
        message: str,
        user: str | None = None,
    ) -> None:
        """
        Send deletion result notification.

        Args:
            resource_id: ID of the deleted resource
            success: Whether deletion was successful
            message: Result message
            user: User who initiated the deletion
        """
        emoji = "✅" if success else "❌"
        status = "successfully deleted" if success else "failed to delete"
        user_text = f" by <@{user}>" if user else ""

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{emoji} Resource `{resource_id}` was {status}{user_text}",
                },
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": message}],
            },
        ]

        try:
            self.client.chat_postMessage(
                channel=self.slack_settings.channel,
                blocks=blocks,
                text=f"Resource {resource_id} {status}",
            )
        except SlackApiError as e:
            self._log.error("slack_send_error", error=str(e))

    def update_message_after_action(
        self,
        channel: str,
        ts: str,
        zombie: ZombieResource,
        action: str,
        user: str,
    ) -> None:
        """
        Update the original message after an action is taken.

        Args:
            channel: Slack channel ID
            ts: Message timestamp
            zombie: The zombie resource
            action: Action taken (deleted, ignored)
            user: User who took the action
        """
        action_text = {
            "deleted": "🗑️ *Deleted*",
            "ignored": "👁️ *Ignored*",
        }

        status_text = action_text.get(action, f"*{action.title()}*")
        type_name = zombie.resource_type.value.replace("_", " ").title()

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"~{type_name}: `{zombie.id}`~\n{status_text} by <@{user}>",
                },
            },
        ]

        try:
            self.client.chat_update(
                channel=channel,
                ts=ts,
                blocks=blocks,
                text=f"Resource {zombie.id} {action}",
            )
        except SlackApiError as e:
            self._log.error("slack_update_error", error=str(e))
