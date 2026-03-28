"""Claude API client wrapper using Anthropic SDK."""

from collections.abc import AsyncIterator
from typing import Optional

import anthropic
from loguru import logger

from core.config import settings


class ClaudeClient:
    """Async wrapper around Anthropic Messages API."""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        kwargs: dict = {}
        # Support both ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN
        key = api_key or settings.anthropic_api_key or settings.anthropic_auth_token
        if key:
            kwargs["api_key"] = key
        url = base_url or settings.anthropic_base_url
        if url:
            kwargs["base_url"] = url
        self._client = anthropic.AsyncAnthropic(**kwargs)

    async def chat(
        self,
        messages: list[dict],
        system: str = "",
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 4096,
    ) -> str:
        """Send messages and return the full response text."""
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system

        try:
            response = await self._client.messages.create(**kwargs)
            text = response.content[0].text if response.content else ""
            logger.info(
                "Claude API call: model={}, input_tokens={}, output_tokens={}",
                model, response.usage.input_tokens, response.usage.output_tokens,
            )
            return text
        except Exception as e:
            logger.exception("Claude API error: {}", e)
            raise

    async def chat_stream(
        self,
        messages: list[dict],
        system: str = "",
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        """Stream response text chunks."""
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system

        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    yield text
        except Exception as e:
            logger.exception("Claude API stream error: {}", e)
            raise


# Singleton
claude_client = ClaudeClient()
