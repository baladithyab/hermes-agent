"""State-free model-probe result formatting and endpoint cache identity."""

from typing import Any, Optional


def probe_negative_cache_key(base_url: str) -> Optional[str]:
    """``host:port`` for *base_url* (both URL candidates share one entry), or None without a host."""
    from utils import base_url_origin

    _, host, port = base_url_origin(base_url)
    return f"{host}:{port}" if host else None


def probe_result(
    models, probed_url, resolved_base_url, suggested_base_url=None, used_fallback=False
) -> dict[str, Any]:
    return {
        "models": models,
        "probed_url": probed_url,
        "resolved_base_url": resolved_base_url,
        "suggested_base_url": suggested_base_url,
        "used_fallback": used_fallback}
