"""Downloadable bounded diagnostics with values and action payloads redacted."""

from collections.abc import Mapping
import inspect
import re
from typing import Any

REDACTED = "**REDACTED**"
_PRIVATE_KEYS = {"token", "password", "secret", "api_key", "access_token", "refresh_token", "authorization", "headers", "cookie", "cookies", "username", "user_id", "host", "hostname", "ip", "mac", "address", "latitude", "longitude", "webhook_id", "media_content_id", "media_id", "url", "uri", "data", "target", "name", "title", "source", "source_list", "message", "error", "exception"}
_VALUE_KEYS = {"value", "desired", "observed", "expected", "allowed_values", "value_map"}
_URL = re.compile(r"(?:https?|rtsp|mqtt|file|builtin)://", re.I)


def redact_diagnostics(value: Any, key: str | None = None) -> Any:
    """Keep topology, reasons and timing; omit user payloads and credentials.

    Diagnostic JSON is intentionally not a configuration backup. Export is a
    separate explicit action and can contain private entity and action data.
    """
    if key and (key.lower() in _PRIVATE_KEYS or any(part in key.lower() for part in ("password", "secret", "token"))):
        return REDACTED
    if key in _VALUE_KEYS:
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        return REDACTED
    if isinstance(value, Mapping):
        return {str(k): redact_diagnostics(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact_diagnostics(v) for v in value]
    if isinstance(value, str) and _URL.search(value):
        return REDACTED
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(type(value).__name__)


async def async_get_config_entry_diagnostics(hass, entry):
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is None:
        return {"configuration": redact_diagnostics(entry.data.get("config", {})), "loaded": False}
    if hasattr(coordinator, "async_diagnostics"):
        result = coordinator.async_diagnostics()
        if inspect.isawaitable(result):
            result = await result
    else:
        result = {"configuration": coordinator.config, "snapshot": coordinator.snapshot}
    return redact_diagnostics(result)
