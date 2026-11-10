"""Comment-preserving config publication with omission guards and policy epochs."""

from pathlib import Path
from typing import Any, Optional


def write_config_state(
    config_path: Path, data: dict[str, Any], *, allow_omissions: bool,
    extra_content_on_create: Optional[str] = None,
) -> None:
    """Shared comment-preserving config writer; omission policy is selected by the public wrapper."""
    from hermes_cli.config import (
        _omitted_config_paths, _refuse_failed_read, _refuse_overwrite,
        require_readable_config_before_write, write_config_document)
    _refuse_failed_read(config_path, data)
    # Guard reads must preserve the caller's remote-config read base.
    from hermes_cli.config_backend import keeping_read_base
    existing = keeping_read_base(lambda: require_readable_config_before_write(config_path))
    if not allow_omissions:
        omitted = _omitted_config_paths(existing, data)
        if omitted:
            shown = ", ".join(omitted[:12])
            if len(omitted) > 12:
                shown += f", +{len(omitted) - 12} more"
            exc = ValueError(f"omitted config paths: {shown}")
            raise _refuse_overwrite(
                config_path,
                "would lose settings omitted by this write",
                exc,
                "Pass the complete current config, or use atomic_config_replace() only when "
                "deletion by omission is deliberate.",
            ) from exc
    from hermes_cli.plugin_policy_epoch import record_policy_write
    record_policy_write(config_path.parent, existing, data)
    write_config_document(config_path, data, extra_content_on_create=extra_content_on_create)
