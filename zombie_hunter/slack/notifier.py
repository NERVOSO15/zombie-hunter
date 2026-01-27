"""Slack notification module for zombie resource alerts using async aiohttp."""

import json
from typing import Any

import aiohttp
import structlog

from zombie_hunter.config import Settings, SlackMode
from zombie_hunter.cost.estimator import CostEstimator
from zombie_hunter.resources.types import (
    AggregatedScanResult,
    ResourceType,
    ZombieResource,
)

logger = structlog.get_logger()

# Slack Block Kit limits
MAX_BLOCKS_PER_MESSAGE = 50
SAFE_BLOCKS_PER_MESSAGE = 40  # Leave buffer for safety

SLACK_API_URL = "https://slack.com/api"


class SlackNotifier:
    """
    Sends zombie resource notifications to Slack using async aiohttp.

    Features:
    - Non-blocking async HTTP calls via aiohttp
    - Automatic pagination for large block payloads (>40 blocks)
    - Resilient error handling
    """

    def __init__(self, settings: Settings) -> None:
        """
        Initialize Slack notifier.

        Args:
            settings: Application settings
        """
        self.settings = settings
        self.slack_settings = settings.slack
        self.bot_token = self.slack_settings.bot_token
        self.cost_estimator = CostEstimator()
        self._log = logger.bind(component="slack_notifier")

    def _get_headers(self) -> dict[str, str]:
        """Get authorization headers for Slack API."""
        return {
            "Authorization": f"Bearer {self.bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    async def _post_message(
        self,
        session: aiohttp.ClientSession,
        blocks: list[dict[str, Any]],
        text: str,
        channel: str | None = None,
    ) -> bool:
        """
        Post a message to Slack using aiohttp.

        Args:
            session: aiohttp client session
            blocks: Slack Block Kit blocks
            text: Fallback text
            channel: Channel to post to (defaults to configured channel)

        Returns:
            True if successful, False otherwise
        """
        payload = {
            "channel": channel or self.slack_settings.channel,
            "blocks": blocks,
            "text": text,
        }

        try:
            async with session.post(
                f"{SLACK_API_URL}/chat.postMessage",
                headers=self._get_headers(),
                json=payload,
            ) as response:
                data = await response.json()

                if not data.get("ok"):
                    self._log.error(
                        "slack_api_error",
                        error=data.get("error"),
                        response_metadata=data.get("response_metadata"),
                    )
                    return False

                return True

        except aiohttp.ClientError as e:
            self._log.error("slack_http_error", error=str(e))
            return False

    async def _update_message(
        self,
        session: aiohttp.ClientSession,
        channel: str,
        ts: str,
        blocks: list[dict[str, Any]],
        text: str,
    ) -> bool:
        """
        Update an existing Slack message.

        Args:
            session: aiohttp client session
            channel: Channel ID
            ts: Message timestamp
            blocks: New blocks
            text: Fallback text

        Returns:
            True if successful
        """
        payload = {
            "channel": channel,
            "ts": ts,
            "blocks": blocks,
            "text": text,
        }

        try:
            async with session.post(
                f"{SLACK_API_URL}/chat.update",
                headers=self._get_headers(),
                json=payload,
            ) as response:
                data = await response.json()
                return data.get("ok", False)

        except aiohttp.ClientError as e:
            self._log.error("slack_update_error", error=str(e))
            return False

    async def _send_paginated_blocks(
        self,
        session: aiohttp.ClientSession,
        all_blocks: list[dict[str, Any]],
        header_blocks: list[dict[str, Any]],
        footer_blocks: list[dict[str, Any]],
        fallback_text: str,
    ) -> bool:
        """
        Send blocks with automatic pagination if exceeding Slack limits.

        Logic:
        - First message includes header_blocks + first chunk of content
        - Middle messages contain content chunks only
        - Last message includes final content chunk + footer_blocks

        Args:
            session: aiohttp client session
            all_blocks: All content blocks to send
            header_blocks: Blocks for the first message header
            footer_blocks: Blocks for the last message footer
            fallback_text: Fallback text for notifications

        Returns:
            True if all messages sent successfully
        """
        if not all_blocks:
            # Just send header + footer
            combined = header_blocks + footer_blocks
            return await self._post_message(session, combined, fallback_text)

        # Calculate available space for content in first/last messages
        header_size = len(header_blocks)
        footer_size = len(footer_blocks)

        # Chunk the content blocks
        chunks: list[list[dict[str, Any]]] = []
        current_chunk: list[dict[str, Any]] = []

        for block in all_blocks:
            current_chunk.append(block)

            # First chunk accounts for header, subsequent chunks use full limit
            limit = SAFE_BLOCKS_PER_MESSAGE - header_size if not chunks else SAFE_BLOCKS_PER_MESSAGE

            if len(current_chunk) >= limit:
                chunks.append(current_chunk)
                current_chunk = []

        # Don't forget remaining blocks
        if current_chunk:
            chunks.append(current_chunk)

        # If only one chunk and it fits with header + footer
        total_blocks = header_size + len(all_blocks) + footer_size
        if total_blocks <= SAFE_BLOCKS_PER_MESSAGE:
            combined = header_blocks + all_blocks + footer_blocks
            return await self._post_message(session, combined, fallback_text)

        # Send paginated messages
        success = True
        total_chunks = len(chunks)

        for i, chunk in enumerate(chunks):
            is_first = i == 0
            is_last = i == total_chunks - 1

            message_blocks: list[dict[str, Any]] = []

            # Add header to first message
            if is_first:
                message_blocks.extend(header_blocks)

            # Add content chunk
            message_blocks.extend(chunk)

            # Add continuation indicator if not last
            if not is_last:
                message_blocks.append(
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": f"_Continued in next message... ({i + 1}/{total_chunks})_",
                            }
                        ],
                    }
                )

            # Add footer to last message
            if is_last:
                message_blocks.extend(footer_blocks)

            chunk_text = f"{fallback_text} (Part {i + 1}/{total_chunks})"
            if not await self._post_message(session, message_blocks, chunk_text):
                success = False
                self._log.error("paginated_send_failed", chunk=i + 1, total=total_chunks)

        return success

    async def send_scan_results(self, results: AggregatedScanResult) -> bool:
        """
        Send scan results to Slack asynchronously.

        Args:
            results: Aggregated scan results

        Returns:
            True if notification was sent successfully
        """
        async with aiohttp.ClientSession() as session:
            if not results.all_zombies:
                self._log.info("no_zombies_found", message="No zombies to report")
                return await self._send_no_zombies_message(session)

            try:
                # Send summary report with pagination support
                await self._send_summary_message(session, results)

                # Send individual resource notifications if configured
                if (
                    self.slack_settings.mode == SlackMode.INTERACTIVE
                    and self.slack_settings.post_individual_resources
                ):
                    zombies_to_post = results.all_zombies[
                        : self.slack_settings.max_individual_posts
                    ]

                    for zombie in zombies_to_post:
                        await self._send_zombie_notification(session, zombie)

                    remaining = len(results.all_zombies) - len(zombies_to_post)
                    if remaining > 0:
                        await self._send_remaining_count_message(session, remaining)

                return True

            except Exception as e:
                self._log.error("slack_send_error", error=str(e), error_type=type(e).__name__)
                return False

    async def _send_no_zombies_message(self, session: aiohttp.ClientSession) -> bool:
        """Send a message when no zombies are found."""
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

        return await self._post_message(session, blocks, "No zombie resources found!")

    async def _send_summary_message(
        self, session: aiohttp.ClientSession, results: AggregatedScanResult
    ) -> None:
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

        # Header blocks (first message)
        header_blocks: list[dict[str, Any]] = [
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
        ]

        # Content blocks (paginated)
        content_blocks: list[dict[str, Any]] = [
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

        # Footer blocks (last message)
        footer_blocks: list[dict[str, Any]] = []

        # Add scan metadata to footer
        if results.results:
            regions = []
            for r in results.results:
                regions.extend(r.regions_scanned)
            regions_text = ", ".join(sorted(set(regions)))

            footer_blocks.append(
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

        fallback_text = (
            f"Zombie Hunter found {results.total_zombie_count} zombies "
            f"(${results.total_monthly_savings:.2f}/mo potential savings)"
        )

        await self._send_paginated_blocks(
            session, content_blocks, header_blocks, footer_blocks, fallback_text
        )

    async def _send_zombie_notification(
        self, session: aiohttp.ClientSession, zombie: ZombieResource
    ) -> None:
        """Send individual zombie notification with action buttons."""
        blocks = self._build_zombie_blocks(zombie)

        await self._post_message(
            session, blocks, f"Zombie found: {zombie.resource_type.value} - {zombie.id}"
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

    async def _send_remaining_count_message(
        self, session: aiohttp.ClientSession, remaining: int
    ) -> None:
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

        await self._post_message(session, blocks, f"...and {remaining} more zombie resources")

    async def send_deletion_result(
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

        async with aiohttp.ClientSession() as session:
            await self._post_message(session, blocks, f"Resource {resource_id} {status}")

    async def update_message_after_action(
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

        async with aiohttp.ClientSession() as session:
            await self._update_message(
                session, channel, ts, blocks, f"Resource {zombie.id} {action}"
            )
