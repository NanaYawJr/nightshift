"""The fault contract.

Every fault the injector can produce implements this interface. The contract
matters more than any individual fault: it is what lets the evaluation harness
inject something, wait for detection, and score the result without any part of
the detection path knowing what was done.

Four rules, and they are the whole design:

1. A fault knows its own ground truth. It states, in structured form, what it
   did and what the correct diagnosis would be. That statement is sealed away
   from everything downstream of detection.

2. A fault is reversible. `revert()` must return the estate to its prior state,
   because a benchmark that can only run once is not a benchmark.

3. A fault is deterministic given a seed. The same seed produces the same fault
   on the same target, so a failed diagnosis can be reproduced and debugged.

4. A fault is revertible from its sealed record alone. `from_ground_truth()`
   rebuilds an injected instance from JSON, with no live object required. Four
   separate notebook sessions timed out between injection and revert during
   development, each time stranding the estate until it was restored by hand.
   A thirty-fault benchmark run will outlast any session.

Faults do not touch Spark directly in their constructors. They describe what
they will do, and `inject(spark)` performs it. This keeps the declaration
testable locally without a Fabric runtime.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class GroundTruth:
    """What was actually done. Sealed; never visible to the detection path.

    `mechanism` is the specific thing that changed, stated plainly enough that a
    human scorer can compare it to the agent's diagnosis without interpretation.

    `parameters` must carry everything `_undo` needs, because it is the only
    input `from_ground_truth` has to work from.
    """

    fault_id: str
    fault_class: str
    target_workspace: str
    target_item: str
    mechanism: str
    expected_symptom: str
    injected_at: datetime
    seed: int
    parameters: dict[str, Any] = field(default_factory=dict)
    reverted_at: datetime | None = None

    def to_json(self) -> str:
        payload = asdict(self)
        for key in ("injected_at", "reverted_at"):
            if payload[key] is not None:
                payload[key] = payload[key].isoformat()
        return json.dumps(payload, indent=2)


class Fault(ABC):
    """Base class for every injectable fault."""

    fault_class: str = "unclassified"

    def __init__(self, target_workspace: str, target_item: str, seed: int = 4417):
        self.target_workspace = target_workspace
        self.target_item = target_item
        self.seed = seed
        self._injected_at: datetime | None = None
        self._revert_state: dict[str, Any] = {}

    @property
    def fault_id(self) -> str:
        """Stable across runs with the same seed, so results are comparable.

        Includes a digest of the fault's parameters. Two faults of the same class
        on the same target differ only in their parameters — without the digest
        they collide, and the second silently overwrites the first's sealed
        ground truth.
        """
        digest = hashlib.sha1(
            json.dumps(self.parameters(), sort_keys=True).encode()
        ).hexdigest()[:6]

        return f"{self.fault_class}-{self.target_item}-{digest}-{self.seed}"

    # --- What each fault must declare -------------------------------------

    @abstractmethod
    def mechanism(self) -> str:
        """One sentence: exactly what was changed. Scored against the diagnosis."""

    @abstractmethod
    def expected_symptom(self) -> str:
        """What detection should notice. Not what caused it."""

    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """Everything a scorer needs, and everything `_undo` needs.

        Feeds `fault_id`, so it must distinguish two otherwise identical faults
        on the same target. Feeds `from_ground_truth`, so it must be sufficient
        to rebuild a revertible instance with no other input.
        """

    @abstractmethod
    def _apply(self, spark: Any) -> None:
        """Perform the change. Record whatever `_undo` will need."""

    @abstractmethod
    def _undo(self, spark: Any) -> None:
        """Restore the prior state using `self._revert_state`."""

    @classmethod
    @abstractmethod
    def _rebuild(cls, truth: GroundTruth) -> "Fault":
        """Construct an instance from sealed parameters, with `_revert_state` set.

        The counterpart to `parameters()`. If a fault cannot implement this, its
        parameters are not recording enough.
        """

    # --- Lifecycle ---------------------------------------------------------

    def inject(self, spark: Any) -> GroundTruth:
        if self._injected_at is not None:
            raise RuntimeError(f"{self.fault_id} is already injected")

        self._apply(spark)
        self._injected_at = datetime.now(timezone.utc)

        return self.ground_truth()

    def inject_and_seal(self, spark: Any, seal: "Seal") -> GroundTruth:
        """Inject and record in one step.

        A fault injected but not yet sealed cannot be reverted by anything but
        the live object that created it — which is the failure this whole
        mechanism exists to prevent. Sealing is not an optional follow-up.
        """
        truth = self.inject(spark)
        seal.store(truth)
        return truth

    def revert(self, spark: Any) -> None:
        if self._injected_at is None:
            raise RuntimeError(f"{self.fault_id} was never injected")

        self._undo(spark)
        self._injected_at = None
        self._revert_state = {}

    @classmethod
    def from_ground_truth(cls, truth: GroundTruth) -> "Fault":
        """Rebuild an injected instance from its sealed record.

        The returned object behaves as though it had just been injected: calling
        `revert(spark)` on it undoes the fault. No live session required, so a
        crashed benchmark run can be cleaned up by any process that can read the
        seal.
        """
        if truth.fault_class != cls.fault_class:
            raise ValueError(
                f"{cls.__name__} cannot rebuild a '{truth.fault_class}' fault"
            )

        fault = cls._rebuild(truth)
        fault._injected_at = truth.injected_at
        return fault

    def ground_truth(self) -> GroundTruth:
        if self._injected_at is None:
            raise RuntimeError("Ground truth is only meaningful once injected")

        return GroundTruth(
            fault_id=self.fault_id,
            fault_class=self.fault_class,
            target_workspace=self.target_workspace,
            target_item=self.target_item,
            mechanism=self.mechanism(),
            expected_symptom=self.expected_symptom(),
            injected_at=self._injected_at,
            seed=self.seed,
            parameters=self.parameters(),
        )


class Seal:
    """Writes ground truth somewhere the detection path cannot read.

    The separation is enforced by convention rather than by permission, which is
    weaker than it should be. What makes it hold in practice is that no module
    under `collectors/`, `detection/` or `agent/` imports from `estate.faults` —
    a rule worth checking in CI rather than trusting.
    """

    def __init__(self, path: str | Path = "eval/sealed"):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)

    def store(self, truth: GroundTruth) -> Path:
        """Write sealed ground truth. Refuses to overwrite.

        Scoring depends on these files, so a silent clobber would corrupt a
        benchmark run without any error. Loud failure is the right trade.
        """
        target = self.path / f"{truth.fault_id}.json"

        if target.exists():
            raise FileExistsError(
                f"Ground truth for {truth.fault_id} is already sealed. "
                "Overwriting would corrupt the benchmark silently."
            )

        target.write_text(truth.to_json())
        return target

    def load(self, fault_id: str) -> GroundTruth:
        raw = json.loads((self.path / f"{fault_id}.json").read_text())

        for key in ("injected_at", "reverted_at"):
            if raw.get(key):
                raw[key] = datetime.fromisoformat(raw[key])

        return GroundTruth(**raw)

    def list_ids(self) -> list[str]:
        return sorted(p.stem for p in self.path.glob("*.json"))

    def discard(self, fault_id: str) -> None:
        """Remove a sealed record after its fault has been reverted.

        Leaving it would let a later revert restore a backup that no longer
        matches the estate.
        """
        (self.path / f"{fault_id}.json").unlink(missing_ok=True)