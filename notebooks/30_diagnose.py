# Fabric notebook: 30_diagnose
#
# Asks the agent what caused each incident, asks it what to change, and
# records both. Re-detects rather than reading incidents from the store, so
# the evidence pack is built from the same frames detection used.
#
# This is the only notebook that calls a model, and the only one that needs a
# credential.
#
# WHERE IT LIVES
#   Workspace:   h1-nightshift
#   Lakehouse:   lh_nightshift, pinned as default
#   Environment: env_nightshift (REQUIRED, D-033)
#   Secret:      Files/secrets/model.json in lh_nightshift
#
# THE SECRET. model.json holds the API key, the model name and the base URL.
# It is uploaded through the Fabric UI and never typed into a cell, never
# printed, and never committed. There is no Key Vault on trial capacity — a
# documented deviation in ARCHITECTURE.md, whose production fix is Key Vault
# through a managed identity.
#
# WHAT THIS NOTEBOOK MUST NEVER IMPORT. Nothing from estate.faults. Sealed
# ground truth is read only by 40_review, and the separation between the thing
# that answers and the thing that marks the answer is what makes any score
# here worth reading.
#
# Paste each block below into its own notebook cell, in order.


# ============================================================================
# CELL 1 — Fetch the repo
# ============================================================================
#
# Identical in 10_collect, 20_detect, 30_diagnose and 40_review.

import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = "NanaYawJr/nightshift"
BRANCH = "main"

work = Path("/tmp/nightshift-diagnose")
if work.exists():
    shutil.rmtree(work)
work.mkdir(parents=True)

archive = work / "repo.zip"
urllib.request.urlretrieve(
    f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip", archive
)
with zipfile.ZipFile(archive) as zf:
    zf.extractall(work)

package_root = next(p for p in work.iterdir() if p.is_dir())

if str(package_root) not in sys.path:
    sys.path.insert(0, str(package_root))

# Anything already imported from a previous run is stale (D-040), and Python
# will keep using it however many times this cell re-downloads the code. The
# package list is derived from the download rather than written by hand
# (D-048), because a hand-written list omitted `eval` and a notebook spent an
# afternoon scoring with replaced code.
OURS = {p.name for p in package_root.iterdir() if (p / "__init__.py").exists()}

stale = [name for name in sys.modules if name.split(".")[0] in OURS]
for name in stale:
    del sys.modules[name]

try:
    import pydantic_settings  # noqa: F401
except ImportError as exc:
    raise RuntimeError(
        "pydantic-settings is missing. Select the env_nightshift Environment "
        "in the ribbon (D-033)."
    ) from exc

print(f"repo at {package_root}")
print(f"packages: {sorted(OURS)}")
print(f"stale modules cleared: {len(stale)}")


# ============================================================================
# CELL 2 — Model credentials
# ============================================================================

import json

from common.config import settings

# Read from a file in the Lakehouse, never typed into a cell and never in git.
SECRETS = "/lakehouse/default/Files/secrets/model.json"

with open(SECRETS) as handle:
    secrets = json.load(handle)

settings.model_api_key = secrets["api_key"]
settings.model_name = secrets["model_name"]
settings.model_base_url = secrets["base_url"]

# The key itself is never printed. Its length is enough to confirm the file was
# read and parsed without putting the secret in the notebook's saved output —
# and a notebook's output is saved, which is the whole reason for the care.
print(f"model:    {settings.model_name}")
print(f"endpoint: {settings.model_base_url}")
print(f"key:      {len(settings.model_api_key)} chars")


# ============================================================================
# CELL 3 — Detect
# ============================================================================

from common import storage
from detection import telemetry
from detection.rules import silent_zero_row
from detection.rules.silent_zero_row import WriteExpectation

storage.use_lakehouse(spark, prefix="tel_")

runs = telemetry.load("job_runs")
commits = telemetry.load("delta_commits")

# What the pipeline was given to copy. Empty for incidents predating the
# sampler, and the pack says so rather than implying the source was fine —
# which is the distinction D-038 is about.
landing = telemetry.load("landing_files")

# Printed before anything else is believed. A collector that stopped makes
# every number below a measurement of a frozen snapshot, which went unnoticed
# for 38 hours (D-033).
print(f"collector last wrote {runs['collected_at'].max()}")
print(f"newest run observed  {runs['start_time'].max()}")
print()

EXPECTATIONS = [
    WriteExpectation(
        pipeline="pl_ingest_ledger_daily",
        table="ledger_daily",
        workspace="h1-finance-prod",
        minimum_rows=1,
    ),
]

incidents = silent_zero_row.detect(
    runs=runs,
    commits=commits,
    expectations=EXPECTATIONS,
    evaluate_before=telemetry.complete_until(commits),
)

print(f"{len(incidents)} incidents to diagnose")
print(f"{len(landing)} source samples available")


# ============================================================================
# CELL 4 — One incident, end to end
# ============================================================================
#
# Prints the whole evidence pack. Worth running on its own before any batch:
# the pack is the input the diagnosis is judged against, and reading it is the
# only way to tell a reasoning failure from a coverage failure.

from agent.agents import diagnostician
from agent.tools import evidence

# The most recent incident, because it is the one whose cause is freshest in
# your memory and therefore the one you can actually check the answer against.
incident = max(incidents, key=lambda i: i.measured["run_started"])

pack = evidence.build(incident, runs, commits, landing)

print(f"EVIDENCE PACK — {pack.incident_id}")
print("-" * 70)
print(pack.as_prompt())
print()

diagnosis = diagnostician.diagnose(pack, incident.fault_class)

print("DIAGNOSIS")
print("-" * 70)
print(diagnosis)
print()
print(f"source: {diagnosis.source}")

# Empty on a real diagnosis, and on an abstention. Populated means the model
# was unreachable, replied unparseably, or cited a fact that does not exist —
# and says which. Abstention and degradation are different outcomes (D-039).
if diagnosis.fallback_reason:
    print(f"fell back because: {diagnosis.fallback_reason}")


# ============================================================================
# CELL 5 — A sample, not all of them
# ============================================================================

import time
from collections import Counter

from agent.agents import remediator

# Capped deliberately. Seventy-odd incidents is a handful of outages, and
# diagnosing every hourly run of the same outage spends a free-tier quota to
# get the same answer dozens of times. Grouping consecutive incidents into one
# event is the next design question; until it is answered, a sample is the
# honest way to look at quality.
SAMPLE = 6
PAUSE_SECONDS = 3

sample = incidents[:: max(1, len(incidents) // SAMPLE)][:SAMPLE]

results = []
proposals = []

for one in sample:
    pack = evidence.build(one, runs, commits, landing)

    diagnosis = diagnostician.diagnose(pack, one.fault_class)
    results.append(diagnosis)
    time.sleep(PAUSE_SECONDS)  # free tiers count requests per minute

    # Called even on an abstention. The remediator declines by itself in that
    # case, and the decline is the record that it was asked and said no —
    # which is the difference between a system that withheld a fix and one
    # that was never consulted.
    proposals.append(remediator.propose(pack, diagnosis))
    time.sleep(PAUSE_SECONDS)

print(f"{len(results)} diagnosed")
print()
print("by source:     ", dict(Counter(r.source for r in results)))
print("by confidence: ", dict(Counter(r.confidence for r in results)))
print()

for result in results:
    print(result)
    if result.fallback_reason:
        print(f"  fell back: {result.fallback_reason}")
    print()

print("=" * 70)
print("PROPOSALS")
print("=" * 70)
print("by target:", dict(Counter(p.target for p in proposals)))
print()

for proposal in proposals:
    if proposal.target == "none":
        print(f"{proposal.incident_id}: no change — {proposal.rationale}")
    else:
        print(proposal)
    print()


# ============================================================================
# CELL 6 — Persist
# ============================================================================

from store import incidents as store

# Every incident is recorded, not only the sampled ones. An incident without a
# diagnosis is the normal state — detection runs hourly and diagnosis does not
# — and the store is built so a diagnosis arriving later attaches to the row
# already there rather than creating a second one.
diagnosed = {d.incident_id: d for d in results}
proposed = {p.incident_id: p for p in proposals}

written = store.record(incidents, diagnosed, proposed)

print(f"{written} rows appended")
print()
print("store:", store.summarise())
print()

# The review queue. Nothing here has been ruled on by a human yet, and the
# fix-acceptance figure in EVALUATION.md is built entirely from those rulings.
queue = store.pending()

print(f"{len(queue)} awaiting a decision")
print()
print(
    store.to_rows(
        queue.head(10),
        ["incident_id", "detected_at", "severity", "diagnosis_cause"],
    )
)


# ============================================================================
# APPENDIX A — Diagnose a named list of incidents
# ============================================================================
#
# Not part of the normal run. Cell 5 samples across the whole queue, which for
# a store full of seeded history means it reaches the oldest incidents first —
# and those predate the source sampler, so they cannot be scored against a
# seal. This cell diagnoses the ones that can.
#
# The ids come from 40_review, which is the only notebook allowed to open the
# seal. Passing ids across rather than importing Seal here keeps ground truth
# out of the diagnosis path: an id says which run to look at, it does not say
# what was done to it. DO NOT add a Seal import to this notebook.
#
# The list below is the 15 October controlled injection, kept as a worked
# example. Replace it with whatever 40_review Cell 6 reports as matched.

from agent.agents import diagnostician, remediator
from agent.tools import evidence
from store import incidents as store

TARGETS = [
    "INC-8d4a726f07",   # 15 Oct 15:46
    "INC-e6b2a41dc2",   # 15 Oct 16:46
    "INC-eeb82d5d0c",   # 15 Oct 17:46
    "INC-b5bedd44bf",   # 15 Oct 18:46
    "INC-c34220bc66",   # 15 Oct 19:46
]

selected = [i for i in incidents if i.incident_id in TARGETS]

# Loud rather than quiet. A target the detector did not reproduce means the
# detection window has moved, and silently diagnosing four of five would put
# a hole in the benchmark that nothing downstream would report.
missing = set(TARGETS) - {i.incident_id for i in selected}
if missing:
    raise RuntimeError(f"not produced by this detection run: {sorted(missing)}")

diagnoses, proposals = {}, {}

for incident in selected:
    pack = evidence.build(incident, runs, commits, landing)

    diagnosis = diagnostician.diagnose(pack, incident.fault_class)
    diagnoses[incident.incident_id] = diagnosis
    proposals[incident.incident_id] = remediator.propose(pack, diagnosis)

    print(f"{incident.incident_id}  [{diagnosis.source}]")
    print(f"  {diagnosis.cause}")
    print()

print(f"{store.record(selected, diagnoses, proposals)} rows appended")
print("store:", store.summarise())
