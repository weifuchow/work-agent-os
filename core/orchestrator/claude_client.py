"""LLM client wrapper — all models routed through Anthropic-compatible proxy."""

from collections.abc import AsyncIterator
from typing import Any, Optional

import anthropic
from loguru import logger

from core.config import load_models_config, get_model_override, settings


class ClaudeClient:
    """Async wrapper around Anthropic-compatible API proxy."""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        anthropic_kwargs: dict[str, Any] = {}
        key = api_key or settings.anthropic_api_key or settings.anthropic_auth_token
        if key:
            anthropic_kwargs["api_key"] = key
        url = base_url or settings.anthropic_base_url
        if url:
            anthropic_kwargs["base_url"] = url
        self._anthropic = anthropic.AsyncAnthropic(**anthropic_kwargs)

    def _models_config(self) -> dict[str, Any]:
        return load_models_config()

    def _resolve_model(self, model: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
        config = self._models_config()
        # Priority: explicit param > runtime override > yaml default
        selected_id = model or get_model_override() or config.get("default")
        if not selected_id:
            raise ValueError("No default model configured in data/models.yaml")

        models = [item for item in config.get("models", []) if item.get("enabled", True)]
        match = next((item for item in models if item.get("id") == selected_id), None)
        if match:
            return match, config

        # Unknown model — all go through anthropic (proxy handles routing)
        return {
            "id": selected_id,
            "label": selected_id,
            "enabled": True,
        }, config

    async def _create_anthropic_message(
        self,
        *,
        messages: list[dict],
        system: str,
        model_id: str,
        max_tokens: int,
    ):
        kwargs: dict[str, Any] = {
            "model": model_id,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        response = await self._anthropic.messages.create(**kwargs)
        return response, model_id

    async def _create_message(
        self,
        *,
        messages: list[dict],
        system: str,
        model: str | None,
        max_tokens: int,
    ):
        selected_model, config = self._resolve_model(model)
        model_id = selected_model["id"]

        try:
            return await self._create_anthropic_message(
                messages=messages,
                system=system,
                model_id=model_id,
                max_tokens=max_tokens,
            )
        except anthropic.APIStatusError as e:
            fallback_id = config.get("fallback")
            should_fallback = (
                e.status_code in {429, 500, 529}
                and fallback_id
                and fallback_id != model_id
            )
            if not should_fallback:
                raise

            logger.warning(
                "LLM call failed on model={}, status_code={}, retrying with fallback={}",
                model_id,
                e.status_code,
                fallback_id,
            )
            return await self._create_anthropic_message(
                messages=messages,
                system=system,
                model_id=fallback_id,
                max_tokens=max_tokens,
            )

    async def chat(
        self,
        messages: list[dict],
        system: str = "",
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> str:
        """Send messages and return the full response text."""
        try:
            response, used_model = await self._create_message(
                messages=messages,
                system=system,
                model=model,
                max_tokens=max_tokens,
            )

            text = response.content[0].text if response.content else ""
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens

            self.last_usage = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "model": used_model,
            }
            logger.info(
                "LLM API call: model={}, input_tokens={}, output_tokens={}",
                used_model, input_tokens, output_tokens,
            )
            return text
        except Exception as e:
            logger.exception("LLM API error: {}", e)
            raise

    async def chat_stream(
        self,
        messages: list[dict],
        system: str = "",
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        """Stream response text chunks."""
        selected_model, config = self._resolve_model(model)
        model_id = selected_model["id"]

        kwargs: dict[str, Any] = {
            "model": model_id,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system

        try:
            async with self._anthropic.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    yield text
        except anthropic.APIStatusError as e:
            fallback_id = config.get("fallback")
            should_fallback = (
                e.status_code in {429, 500, 529}
                and fallback_id
                and fallback_id != model_id
            )
            if not should_fallback:
                logger.exception("LLM API stream error: {}", e)
                raise

            logger.warning(
                "LLM API stream failed on model={}, status_code={}, retrying with fallback={}",
                model_id,
                e.status_code,
                fallback_id,
            )
            fallback_kwargs = {**kwargs, "model": fallback_id}
            async with self._anthropic.messages.stream(**fallback_kwargs) as stream:
                async for text in stream.text_stream:
                    yield text
        except Exception as e:
            logger.exception("LLM API stream error: {}", e)
            raise


# Singleton
claude_client = ClaudeClient()
