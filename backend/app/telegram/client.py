from typing import Any

import httpx

from app.core.config import Settings

PHOTO_CAPTION_MAX_LENGTH = 1024


class TelegramDeliveryError(Exception):
    def __init__(
        self,
        message: str,
        *,
        error_class: str = "TelegramAPIError",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.retryable = retryable


class TelegramClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def send_message(
        self,
        destination: str,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        reply_to_message_id: int | None = None,
        disable_web_page_preview: bool = False,
    ) -> str:
        if (
            self.settings.telegram_bot_token is None
            or not self.settings.telegram_bot_token.get_secret_value().strip()
        ):
            raise TelegramDeliveryError(
                "Telegram delivery is not configured",
                error_class="ConfigurationError",
            )
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.http_timeout_seconds,
                trust_env=False,
            ) as client:
                data = await self._request(
                    client,
                    "sendMessage",
                    {
                        "chat_id": destination,
                        "text": text,
                        "disable_web_page_preview": disable_web_page_preview,
                        **({"reply_markup": reply_markup} if reply_markup else {}),
                        **(
                            {"reply_parameters": {"message_id": reply_to_message_id}}
                            if reply_to_message_id is not None
                            else {}
                        ),
                    },
                )
        except httpx.HTTPError as exc:
            retryable = isinstance(
                exc,
                (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError),
            )
            if isinstance(exc, httpx.HTTPStatusError):
                retryable = exc.response.status_code in {408, 425, 429} or (
                    exc.response.status_code >= 500
                )
            raise TelegramDeliveryError(
                f"Telegram delivery failed ({type(exc).__name__})",
                error_class=type(exc).__name__,
                retryable=retryable,
            ) from exc
        except ValueError as exc:
            raise TelegramDeliveryError(
                "Telegram returned an invalid response",
                error_class="InvalidResponse",
            ) from exc

        result = data.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if data.get("ok") is not True or message_id is None:
            raise TelegramDeliveryError(
                "Telegram rejected the message or omitted its message ID",
                error_class="TelegramAPIError",
            )
        return str(message_id)

    async def send_photo(
        self,
        destination: str,
        photo_url: str,
        *,
        caption: str | None = None,
    ) -> str:
        if (
            self.settings.telegram_bot_token is None
            or not self.settings.telegram_bot_token.get_secret_value().strip()
        ):
            raise TelegramDeliveryError(
                "Telegram delivery is not configured",
                error_class="ConfigurationError",
            )
        if caption is not None and len(caption) > PHOTO_CAPTION_MAX_LENGTH:
            raise TelegramDeliveryError(
                "Telegram photo caption exceeds the 1024-character limit",
                error_class="CaptionTooLong",
            )
        payload: dict[str, Any] = {"chat_id": destination, "photo": photo_url}
        if caption is not None:
            payload["caption"] = caption
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.http_timeout_seconds,
                trust_env=False,
            ) as client:
                data = await self._request(client, "sendPhoto", payload)
        except httpx.HTTPError as exc:
            retryable = isinstance(
                exc,
                (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError),
            )
            if isinstance(exc, httpx.HTTPStatusError):
                retryable = exc.response.status_code in {408, 425, 429} or (
                    exc.response.status_code >= 500
                )
            raise TelegramDeliveryError(
                f"Telegram photo delivery failed ({type(exc).__name__})",
                error_class=type(exc).__name__,
                retryable=retryable,
            ) from exc
        except ValueError as exc:
            raise TelegramDeliveryError(
                "Telegram returned an invalid photo response",
                error_class="InvalidResponse",
            ) from exc

        result = data.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if data.get("ok") is not True or message_id is None:
            raise TelegramDeliveryError(
                "Telegram rejected the photo or omitted its message ID",
                error_class="TelegramAPIError",
            )
        return str(message_id)

    async def answer_callback(self, callback_query_id: str, text: str) -> None:
        if (
            self.settings.telegram_bot_token is None
            or not self.settings.telegram_bot_token.get_secret_value().strip()
        ):
            raise TelegramDeliveryError(
                "Telegram delivery is not configured",
                error_class="ConfigurationError",
            )
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.http_timeout_seconds,
                trust_env=False,
            ) as client:
                data = await self._request(
                    client,
                    "answerCallbackQuery",
                    {"callback_query_id": callback_query_id, "text": text[:200]},
                )
        except httpx.HTTPError as exc:
            retryable = isinstance(
                exc,
                (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError),
            )
            if isinstance(exc, httpx.HTTPStatusError):
                retryable = exc.response.status_code in {408, 425, 429} or (
                    exc.response.status_code >= 500
                )
            raise TelegramDeliveryError(
                f"Telegram callback response failed ({type(exc).__name__})",
                error_class=type(exc).__name__,
                retryable=retryable,
            ) from exc
        if data.get("ok") is not True:
            raise TelegramDeliveryError(
                "Telegram rejected the callback response",
                error_class="TelegramAPIError",
            )

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        token = self.settings.telegram_bot_token.get_secret_value()
        response = await client.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload,
        )
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramDeliveryError(
                "Telegram returned an invalid response",
                error_class="InvalidResponse",
            ) from exc
        if not isinstance(data, dict):
            raise TelegramDeliveryError(
                "Telegram returned an invalid response",
                error_class="InvalidResponse",
            )
        return data
