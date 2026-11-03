import httpx

from app.core.config import Settings


class TelegramDeliveryError(Exception):
    def __init__(self, message: str, *, error_class: str = "TelegramAPIError") -> None:
        super().__init__(message)
        self.error_class = error_class


class TelegramClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def send_message(self, destination: str, text: str) -> str:
        if (
            self.settings.telegram_bot_token is None
            or not self.settings.telegram_bot_token.get_secret_value().strip()
        ):
            raise TelegramDeliveryError(
                "Telegram delivery is not configured",
                error_class="ConfigurationError",
            )
        token = self.settings.telegram_bot_token.get_secret_value()
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.http_timeout_seconds,
                trust_env=False,
            ) as client:
                response = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": destination, "text": text, "disable_web_page_preview": False},
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            raise TelegramDeliveryError(
                f"Telegram delivery failed ({type(exc).__name__})",
                error_class=type(exc).__name__,
            ) from exc
        except ValueError as exc:
            raise TelegramDeliveryError(
                "Telegram returned an invalid response",
                error_class="InvalidResponse",
            ) from exc

        result = data.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if not isinstance(data, dict) or data.get("ok") is not True or message_id is None:
            raise TelegramDeliveryError(
                "Telegram rejected the message or omitted its message ID",
                error_class="TelegramAPIError",
            )
        return str(message_id)
