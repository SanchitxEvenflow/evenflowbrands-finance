import logging

import requests

from config import settings

logger = logging.getLogger("grn_push.slack")


def notify(message: str) -> None:
    """Best-effort side notification — never raises, never blocks the push flow it's called from."""
    logger.info("SLACK: %s", message)
    if not settings.slack_webhook_url:
        return
    try:
        requests.post(settings.slack_webhook_url, json={"text": message}, timeout=5)
    except requests.exceptions.RequestException:
        logger.exception("Slack notify failed")
