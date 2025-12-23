import asyncio
from typing import Any, Dict
import logging

import boto3

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class EmailService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = boto3.client(
            "ses",
            region_name=self.settings.aws_ses_region,
            aws_access_key_id=self.settings.aws_ses_access_key_id,
            aws_secret_access_key=self.settings.aws_ses_secret_access_key,
        )

    async def send_email(self, to_address: str, subject: str, body: str) -> Dict[str, Any]:
        def _send() -> Dict[str, Any]:
            return self._client.send_email(
                Source=self.settings.aws_ses_sender_email,
                Destination={"ToAddresses": [to_address]},
                Message={
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                },
            )

        logger.info("Sending email to %s with subject '%s'", to_address, subject)
        return await asyncio.to_thread(_send)
