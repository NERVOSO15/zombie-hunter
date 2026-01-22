"""Slack interactive component handler for zombie deletion approval."""

import json
import os
from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import structlog

from zombie_hunter.config import Settings, get_global_settings
from zombie_hunter.resources.types import CloudProvider, ResourceType, ZombieResource, ZombieReason
from zombie_hunter.scanners.base import ScannerRegistry
from zombie_hunter.slack.notifier import SlackNotifier

logger = structlog.get_logger()


class SlackInteractiveHandler:
    """Handles Slack interactive components for zombie resource management."""

    def __init__(self, settings: Settings | None = None) -> None:
        """
        Initialize the interactive handler.

        Args:
            settings: Application settings
        """
        self.settings = settings or get_global_settings()
        self._log = logger.bind(component="slack_interactive")

        # Initialize Slack Bolt app
        self.app = App(
            token=self.settings.slack.bot_token,
            signing_secret=self.settings.slack.signing_secret,
        )

        self.notifier = SlackNotifier(self.settings)

        # Register action handlers
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Register Slack action handlers."""

        @self.app.action("delete_zombie")
        def handle_delete(ack: Any, body: dict, client: Any) -> None:
            """Handle delete button click."""
            ack()
            self._handle_delete_action(body, client)

        @self.app.action("ignore_zombie")
        def handle_ignore(ack: Any, body: dict, client: Any) -> None:
            """Handle ignore button click."""
            ack()
            self._handle_ignore_action(body, client)

        @self.app.action("view_details")
        def handle_details(ack: Any, body: dict, client: Any) -> None:
            """Handle view details button click."""
            ack()
            self._handle_details_action(body, client)

    def _handle_delete_action(self, body: dict, client: Any) -> None:
        """
        Handle the delete zombie action.

        Args:
            body: Slack action body
            client: Slack client
        """
        try:
            action = body["actions"][0]
            value = json.loads(action["value"])
            user_id = body["user"]["id"]
            channel = body["channel"]["id"]
            message_ts = body["message"]["ts"]

            resource_id = value["resource_id"]
            resource_type = ResourceType(value["resource_type"])
            provider = CloudProvider(value["provider"])
            region = value["region"]

            self._log.info(
                "delete_action_received",
                resource_id=resource_id,
                user=user_id,
                provider=provider.value,
            )

            # Create a minimal zombie resource for deletion
            zombie = ZombieResource(
                id=resource_id,
                provider=provider,
                resource_type=resource_type,
                region=region,
                reason=ZombieReason.UNUSED,
            )

            # Get the appropriate scanner
            try:
                scanner = ScannerRegistry.get_scanner(provider, self.settings)
            except ValueError as e:
                self._log.error("scanner_not_found", provider=provider.value, error=str(e))
                self._send_error_response(client, channel, message_ts, str(e))
                return

            # Perform deletion
            success, message = scanner.safe_delete(zombie)

            # Update the original message
            action_status = "deleted" if success else "delete_failed"
            self._update_message(client, channel, message_ts, zombie, action_status, user_id)

            # Send result notification
            self.notifier.send_deletion_result(resource_id, success, message, user_id)

            self._log.info(
                "delete_action_completed",
                resource_id=resource_id,
                success=success,
                user=user_id,
            )

        except Exception as e:
            self._log.error("delete_action_error", error=str(e))
            self._send_error_ephemeral(body, client, f"Error processing delete: {str(e)}")

    def _handle_ignore_action(self, body: dict, client: Any) -> None:
        """
        Handle the ignore zombie action.

        Args:
            body: Slack action body
            client: Slack client
        """
        try:
            action = body["actions"][0]
            value = json.loads(action["value"])
            user_id = body["user"]["id"]
            channel = body["channel"]["id"]
            message_ts = body["message"]["ts"]

            resource_id = value["resource_id"]
            resource_type = ResourceType(value["resource_type"])
            provider = CloudProvider(value["provider"])
            region = value["region"]

            self._log.info(
                "ignore_action_received",
                resource_id=resource_id,
                user=user_id,
            )

            # Create zombie for message update
            zombie = ZombieResource(
                id=resource_id,
                provider=provider,
                resource_type=resource_type,
                region=region,
                reason=ZombieReason.UNUSED,
            )

            # Update the original message
            self._update_message(client, channel, message_ts, zombie, "ignored", user_id)

            # TODO: Optionally store ignored resources to skip in future scans

        except Exception as e:
            self._log.error("ignore_action_error", error=str(e))
            self._send_error_ephemeral(body, client, f"Error processing ignore: {str(e)}")

    def _handle_details_action(self, body: dict, client: Any) -> None:
        """
        Handle the view details action.

        Args:
            body: Slack action body
            client: Slack client
        """
        try:
            action = body["actions"][0]
            value = json.loads(action["value"])
            user_id = body["user"]["id"]

            resource_id = value["resource_id"]
            resource_type = ResourceType(value["resource_type"])
            provider = CloudProvider(value["provider"])
            region = value["region"]

            self._log.info(
                "details_action_received",
                resource_id=resource_id,
                user=user_id,
            )

            # Create zombie resource
            zombie = ZombieResource(
                id=resource_id,
                provider=provider,
                resource_type=resource_type,
                region=region,
                reason=ZombieReason.UNUSED,
            )

            # Get scanner and fetch details
            try:
                scanner = ScannerRegistry.get_scanner(provider, self.settings)
                details = scanner.get_resource_details(zombie)
            except ValueError:
                details = {
                    "id": resource_id,
                    "type": resource_type.value,
                    "provider": provider.value,
                    "region": region,
                }

            # Format details as modal
            self._show_details_modal(client, body["trigger_id"], details)

        except Exception as e:
            self._log.error("details_action_error", error=str(e))
            self._send_error_ephemeral(body, client, f"Error fetching details: {str(e)}")

    def _update_message(
        self,
        client: Any,
        channel: str,
        ts: str,
        zombie: ZombieResource,
        action: str,
        user_id: str,
    ) -> None:
        """Update the original message after an action."""
        action_emoji = {
            "deleted": "🗑️",
            "ignored": "👁️",
            "delete_failed": "❌",
        }

        action_text = {
            "deleted": "Deleted",
            "ignored": "Ignored",
            "delete_failed": "Delete Failed",
        }

        emoji = action_emoji.get(action, "❓")
        text = action_text.get(action, action.title())
        type_name = zombie.resource_type.value.replace("_", " ").title()

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"~{type_name}: `{zombie.id}`~",
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"{emoji} *{text}* by <@{user_id}>",
                    }
                ],
            },
        ]

        try:
            client.chat_update(
                channel=channel,
                ts=ts,
                blocks=blocks,
                text=f"Resource {zombie.id} {action}",
            )
        except Exception as e:
            self._log.error("message_update_error", error=str(e))

    def _show_details_modal(self, client: Any, trigger_id: str, details: dict) -> None:
        """Show a modal with resource details."""
        # Format details as blocks
        detail_blocks = []

        for key, value in details.items():
            if value is not None:
                formatted_key = key.replace("_", " ").title()
                if isinstance(value, dict):
                    value_str = json.dumps(value, indent=2)
                elif isinstance(value, bool):
                    value_str = "Yes" if value else "No"
                else:
                    value_str = str(value)

                detail_blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{formatted_key}:*\n`{value_str}`",
                        },
                    }
                )

        view = {
            "type": "modal",
            "title": {"type": "plain_text", "text": "Resource Details"},
            "close": {"type": "plain_text", "text": "Close"},
            "blocks": detail_blocks[:20],  # Slack limit
        }

        try:
            client.views_open(trigger_id=trigger_id, view=view)
        except Exception as e:
            self._log.error("modal_open_error", error=str(e))

    def _send_error_response(
        self, client: Any, channel: str, ts: str, error: str
    ) -> None:
        """Send an error response in the channel."""
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"❌ *Error:* {error}",
                },
            },
        ]

        try:
            client.chat_update(
                channel=channel,
                ts=ts,
                blocks=blocks,
                text=f"Error: {error}",
            )
        except Exception as e:
            self._log.error("error_response_failed", error=str(e))

    def _send_error_ephemeral(self, body: dict, client: Any, error: str) -> None:
        """Send an ephemeral error message to the user."""
        try:
            client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=body["user"]["id"],
                text=f"❌ {error}",
            )
        except Exception as e:
            self._log.error("ephemeral_error_failed", error=str(e))

    def start(self) -> None:
        """Start the Slack app in socket mode."""
        socket_token = os.environ.get("SLACK_APP_TOKEN")
        if not socket_token:
            raise ValueError("SLACK_APP_TOKEN environment variable is required for socket mode")

        self._log.info("starting_slack_handler")
        handler = SocketModeHandler(self.app, socket_token)
        handler.start()

    def get_app(self) -> App:
        """Get the Slack Bolt app for HTTP mode."""
        return self.app


def create_slack_handler(settings: Settings | None = None) -> SlackInteractiveHandler:
    """
    Create a Slack interactive handler.

    Args:
        settings: Application settings

    Returns:
        Configured SlackInteractiveHandler
    """
    return SlackInteractiveHandler(settings)
