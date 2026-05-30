import logging
import os

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("devloop.slack_notify")

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")


def send_notification(
    error_message: str,
    filename: str,
    test_results: dict,
    pr_url: str,
    webhook_url: str = None,
) -> None:
    url = webhook_url or SLACK_WEBHOOK_URL
    if not url:
        logger.warning("No Slack webhook URL — skipping notification")
        return

    test_emoji = "✅ Passing" if test_results.get("passed") else "❌ Failing"

    message = {
        "text": (
            f"🔧 *DevLoop fixed a bug*\n"
            f"*Error:* {error_message}\n"
            f"*File:* `{filename}`\n"
            f"*Tests:* {test_emoji}\n"
            f"*PR:* {pr_url}\n"
            f"_Review and merge when ready._"
        )
    }

    logger.info("Sending Slack notification for PR: %s", pr_url)

    try:
        response = requests.post(
            url,
            json=message,
            timeout=10,
        )
        response.raise_for_status()
        logger.info("Slack notification sent successfully")
    except requests.exceptions.HTTPError as e:
        logger.error("Slack HTTP error: %s — response: %s", e, response.text)
        raise
    except requests.exceptions.RequestException as e:
        logger.error("Slack request failed: %s", e)
        raise
