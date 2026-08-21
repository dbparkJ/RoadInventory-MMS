from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path


def sanitize_name(value: str) -> str:
    """Return the stable filesystem-safe name used for model output folders."""

    safe = re.sub(r"[^0-9A-Za-z._-]+", "_", value)
    safe = safe.strip("._")
    return safe or "item"


def validate_model_output_names(model_names: Iterable[str]) -> None:
    """Reject checkpoint selections that would share an output directory."""

    output_keys: dict[str, str] = {}
    for model_name in model_names:
        name = str(model_name)
        output_key = sanitize_name(Path(name).stem).casefold()
        previous = output_keys.get(output_key)
        if previous is not None:
            raise ValueError(
                "Model names collide after output-path sanitization: "
                f"{previous!r} and {name!r}"
            )
        output_keys[output_key] = name
