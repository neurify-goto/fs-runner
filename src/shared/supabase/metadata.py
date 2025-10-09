from __future__ import annotations

from typing import Any, Dict


def merge_metadata(base: Dict[str, Any] | None, patch: Dict[str, Any] | None) -> Dict[str, Any]:
    """Return a deep-merged metadata dictionary without mutating inputs.

    ``None`` values in ``patch`` are ignored so that existing metadata entries are not
    overwritten with null. Nested dictionaries are merged recursively with the same
    behaviour applied at each level.
    """

    base_copy: Dict[str, Any] = dict(base or {})
    for key, value in (patch or {}).items():
        if value is None:
            continue

        if isinstance(value, dict):
            existing = base_copy.get(key)
            if isinstance(existing, dict):
                base_copy[key] = merge_metadata(existing, value)
            else:
                base_copy[key] = merge_metadata({}, value)
        else:
            base_copy[key] = value
    return base_copy
