"""What a detection rule emits.

One shape for every rule, so the incident store, the agent and the report all
read the same thing regardless of which rule fired.

Incidents are deterministic. Running detection twice over the same telemetry
produces the same `incident_id` for the same underlying event, so re-running is
safe and the store can deduplicate without guessing. The id is derived from the
rule and the specific occurrence — not from the time of detection, which would
make every run produce new rows for old problems.

`measured` holds only facts: numbers read from telemetry, reproducible by
anyone with the same data. Nothing a model asserted belongs here. That
separation is the project's organising idea and it starts at this boundary.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

Severity = Literal["low", "medium", "high"]
Origin = Literal["injected", "discovered"]


@dataclass
class Incident:
    """A detected anomaly, before any diagnosis.

    Deliberately says nothing about cause. A rule reports that something is
    wrong and where; working out why is the agent's job, and conflating the two
    is how a false alarm becomes unexplainable.
    """

    rule: str
    fault_class: str
    workspace: str
    item_name: str
    item_type: str
    severity: Severity
    summary: str
    occurrence_key: str
    measured: dict[str, Any] = field(default_factory=dict)
    detected_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    origin: Origin = "discovered"

    @property
    def incident_id(self) -> str:
        """Stable for a given rule and occurrence.

        `occurrence_key` identifies the specific event — a pipeline run id, a
        Delta version, a date — so the same underlying problem yields the same
        id however many times detection runs. Without it, an hourly detection
        job would file the same incident twenty-four times a day.
        """
        digest = hashlib.sha1(
            f"{self.rule}|{self.workspace}|{self.item_name}|{self.occurrence_key}".encode()
        ).hexdigest()[:10]

        return f"INC-{digest}"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["incident_id"] = self.incident_id
        payload["detected_at"] = self.detected_at.isoformat()
        return payload

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.incident_id} {self.item_name}: {self.summary}"