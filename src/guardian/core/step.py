"""Step output abstraction.

Provides a uniform representation of a pipeline step's output,
with utilities to load from files and convert between formats.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class StepOutput:
    """Represents the output produced by a single pipeline step.

    Attributes:
        step_name: Name of the step that produced this output.
        output_data: The raw output, either as a string or parsed dict.
        timestamp: When the output was captured.
        metadata: Arbitrary key-value metadata about this output.
    """

    step_name: str
    output_data: str | dict
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict = field(default_factory=dict)

    def output_as_string(self) -> str:
        """Return the output as a string.

        If output_data is a dict, serialize it to JSON.
        """
        if isinstance(self.output_data, dict):
            return json.dumps(self.output_data, ensure_ascii=False)
        return self.output_data

    def output_as_dict(self) -> dict | None:
        """Return the output as a dict, or None if not parseable.

        If output_data is already a dict, return it directly.
        If it's a JSON string, parse and return it.
        Otherwise return None.
        """
        if isinstance(self.output_data, dict):
            return self.output_data
        try:
            parsed = json.loads(self.output_data)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
        return None


def load_step_output(
    step_name: str,
    file_path: str,
    metadata: dict | None = None,
) -> StepOutput:
    """Load a step's output from a file.

    Attempts to parse the file as JSON. Falls back to raw text.

    Args:
        step_name: Name of the pipeline step.
        file_path: Path to the output file.
        metadata: Optional metadata dict to attach.

    Returns:
        A StepOutput instance with the loaded data.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Step output file not found: {file_path}")

    raw = path.read_text(encoding="utf-8")

    # Try to parse as JSON dict
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            output_data: str | dict = data
        else:
            output_data = raw
    except (json.JSONDecodeError, TypeError):
        output_data = raw

    return StepOutput(
        step_name=step_name,
        output_data=output_data,
        metadata=metadata or {},
    )
