"""Where collector output goes.

Deliberately a single small seam. Today it writes partitioned Parquet to disk so
collectors can be developed and run offline. In week 3 the Eventhouse arrives and
only this module changes — no collector needs to know where its rows ended up.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

DATA_ROOT = Path("data/raw")


def write(dataset: str, rows: Sequence[dict[str, Any]]) -> Path | None:
    """Append a batch of rows to `dataset`, partitioned by collection date.

    Returns the path written, or None if there was nothing to write.
    """
    if not rows:
        return None

    collected_at = datetime.now(timezone.utc)
    partition = DATA_ROOT / dataset / f"date={collected_at:%Y-%m-%d}"
    partition.mkdir(parents=True, exist_ok=True)

    path = partition / f"{collected_at:%H%M%S}.parquet"

    frame = pd.DataFrame(list(rows))
    frame["collected_at"] = collected_at
    frame.to_parquet(path, index=False)

    return path


def read(dataset: str) -> pd.DataFrame:
    """Read every partition of a dataset back into one frame. For inspection."""
    root = DATA_ROOT / dataset

    if not root.exists():
        return pd.DataFrame()

    files = sorted(root.glob("date=*/*.parquet"))
    if not files:
        return pd.DataFrame()

    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)