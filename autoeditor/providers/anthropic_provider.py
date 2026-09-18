"""Anthropic (Claude) LLM and vision provider.

Uses the official ``anthropic`` SDK with structured outputs
(``output_config.format``) so responses are schema-constrained server-side, and
still validates every response locally before it is trusted. Malformed or
refused responses are retried a bounded number of times.

Credentials come from the environment (``ANTHROPIC_API_KEY``, or an
``ant auth login`` profile); nothing is hard-coded and keys are never logged.
"""

from __future__ import annotations

import base64
import copy
import mimetypes
import os
from pathlib import Path
from typing import Any

from autoeditor.logging_utils import get_logger
from autoeditor.providers.base import LLMResult, MissingCredentialsError, ProviderError
from autoeditor.schemas import LLMJsonError, extract_json_object

log = get_logger(__name__)

DEFAULT_MODEL = "claude-opus-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Keywords the server-side structured-output validator may not accept; they are
# still enforced locally by autoeditor.schemas.validate.
_UNSUPPORTED_KEYS = {"$schema", "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems", "format"}


def simplify_schema_for_output(schema: dict[str, Any]) -> dict[str, Any]:
    """Strip validation keywords that structured outputs do not support."""

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items() if k not in _UNSUPPORTED_KEYS}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    result: dict[str, Any] = walk(copy.deepcopy(schema))
    return result


def _has_credentials() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    # `ant auth login` stores a profile the SDK picks up automatically.
    profile_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "anthropic"
    return profile_dir.exists()


class AnthropicProvider:
    """Implements both LLMProvider and VisionProvider."""

    name = "anthropic"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        effort: str = "high",
        max_json_retries: int = 2,
        refusal_fallbacks: bool = True,
        timeout: float = 600.0,
    ) -> None:
        if not _has_credentials():
            raise MissingCredentialsError("No Anthropic credentials found. Set ANTHROPIC_API_KEY (or run `ant auth login`), or use --mock / AUTOEDITOR_MOCK=1.")
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency is in requirements
            raise ProviderError("The `anthropic` package is not installed (pip install anthropic)") from exc
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(timeout=timeout, max_retries=3)
        self.model = model
        self.effort = effort
        self.max_json_retries = max(0, int(max_json_retries))
        self.refusal_fallbacks = refusal_fallbacks

    # ------------------------------------------------------------------ #
    def _request(self, *, system: str, messages: list[dict[str, Any]], output_schema: dict[str, Any], max_tokens: int) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": simplify_schema_for_output(output_schema)},
            },
        }
        if self.refusal_fallbacks:
            # Server-side refusal fallbacks: if a safety classifier declines, the
            # API re-runs the same request on a fallback model in the same call.
            try:
                return self._client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
            except self._anthropic.BadRequestError as exc:
                message = str(exc).lower()
                if "fallback" not in message:
                    raise
                log.warning("Server-side fallbacks rejected by API; retrying without them")
                self.refusal_fallbacks = False
        return self._client.messages.create(**kwargs)

    def _complete(
        self,
        *,
        system: str,
        content: list[dict[str, Any]],
        output_schema: dict[str, Any],
        max_tokens: int,
        validator: Any,
    ) -> LLMResult:
        messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
        attempts = 0
        last_error: Exception | None = None
        while attempts <= self.max_json_retries:
            attempts += 1
            try:
                response = self._request(system=system, messages=messages, output_schema=output_schema, max_tokens=max_tokens)
            except self._anthropic.AuthenticationError as exc:
                raise MissingCredentialsError("Anthropic rejected the API key") from exc
            except (self._anthropic.RateLimitError, self._anthropic.APIStatusError, self._anthropic.APIConnectionError) as exc:
                raise ProviderError(f"Anthropic API error: {type(exc).__name__}") from exc

            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                raise ProviderError(f"Model declined the request (refusal, category={category})")
            if response.stop_reason == "max_tokens":
                raise ProviderError("Model response was truncated (max_tokens); raise llm.max_tokens")

            text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text")
            usage = getattr(response, "usage", None)
            try:
                data = extract_json_object(text)
                validator(data)
                return LLMResult(
                    data=data,
                    model=getattr(response, "model", self.model),
                    input_tokens=getattr(usage, "input_tokens", 0) or 0,
                    output_tokens=getattr(usage, "output_tokens", 0) or 0,
                    attempts=attempts,
                )
            except (LLMJsonError, ValueError) as exc:
                last_error = exc
                log.warning("Model JSON invalid on attempt %d: %s", attempts, str(exc)[:300])
                # Feed the error back so the next attempt can correct itself.
                messages = [
                    {"role": "user", "content": content},
                    {"role": "assistant", "content": text or "{}"},
                    {
                        "role": "user",
                        "content": (f"That response was not valid against the required JSON schema: {str(exc)[:600]}. Return only a corrected JSON object."),
                    },
                ]
        raise ProviderError(f"Model never returned valid JSON after {attempts} attempts: {last_error}")

    # ------------------------------------------------------------------ #
    def complete_json(self, *, system: str, user: str, output_schema: dict[str, Any], max_tokens: int) -> LLMResult:
        from autoeditor.schemas import Draft202012Validator

        validator = Draft202012Validator(output_schema)

        def check(data: dict[str, Any]) -> None:
            errors = list(validator.iter_errors(data))
            if errors:
                raise ValueError("; ".join(e.message for e in errors[:5]))

        return self._complete(
            system=system,
            content=[{"type": "text", "text": user}],
            output_schema=output_schema,
            max_tokens=max_tokens,
            validator=check,
        )

    def analyze_images(self, *, system: str, user: str, images: list[Path], output_schema: dict[str, Any], max_tokens: int) -> LLMResult:
        from autoeditor.schemas import Draft202012Validator

        if not images:
            raise ProviderError("analyze_images called without images")
        validator = Draft202012Validator(output_schema)

        def check(data: dict[str, Any]) -> None:
            errors = list(validator.iter_errors(data))
            if errors:
                raise ValueError("; ".join(e.message for e in errors[:5]))

        content: list[dict[str, Any]] = []
        for img in images:
            media_type = mimetypes.guess_type(img.name)[0] or "image/jpeg"
            data = base64.standard_b64encode(img.read_bytes()).decode("utf-8")
            content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
        content.append({"type": "text", "text": user})
        return self._complete(system=system, content=content, output_schema=output_schema, max_tokens=max_tokens, validator=check)
