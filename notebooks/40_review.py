# Fabric notebook: 40_review
#
# The human gate, and the only notebook that opens the seal.
#
# Two separate judgements are recorded here, and keeping them apart is the
# point:
#
#   A VERDICT answers "did the diagnosis name the mechanism that was
#   injected". Scored against sealed ground truth. Cells 6 and 7.
#
#   A DECISION answers "would you merge this proposed fix unmodified". Scored
#   against nothing but your own judgement. Cells 3 and 4.
#
# A correct diagnosis can carry an unusable fix, and that combination is the
# most informative thing this project measures — so collapsing the two into
# one number would hide it.
#
# Nothing here changes the estate. Approving a proposal records a judgement;
# it does not execute anything (D-042).
#
# WHERE IT LIVES
#   Workspace:   h1-nightshift
#   Lakehouse:   lh_nightshift, pinned as default
#   Environment: env_nightshift (REQUIRED, D-033)
#   Seal:        Files/sealed in lh_nightshift
#
# WHY THE SEAL LIVES HERE. Ground truth sits beside the incident store rather
# than beside the estate it describes, for the same reason telemetry does
# (D-029): the records have to outlive the estate they are about. It is copied
# into lh_nightshift at injection time by the injection notebook.
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

work = Path("/tmp/nightshift-review")
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
# will keep using it however many times this cell re-downloads the code.
#
# The package list is derived from what was just downloaded rather than
# written here by hand (D-048). The hand-written version omitted `eval`, so
# eval.harness survived every re-run of this cell and this notebook spent an
# afternoon scoring with code that had been replaced on main hours before. A
# list maintained by hand will be wrong again; one derived from the repo
# cannot be.
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

# Printed so the next time this silently fails, it doesn't.
print(f"repo at {package_root}")
print(f"packages: {sorted(OURS)}")
print(f"stale modules cleared: {len(stale)}")


# ============================================================================
# CELL 2 — The queue
# ============================================================================

from common import storage
from store import incidents as store

storage.use_lakehouse(spark, prefix="tel_")

print("store:", store.summarise())
print()

queue = store.pending()

# Only incidents carrying a proposal can be reviewed. The rest are waiting on
# diagnosis, and a reviewer ruling on those would be scoring the detection
# rule rather than the agent.
if "proposal_target" in queue.columns:
    reviewable = queue[queue["proposal_target"].notna()]
else:
    reviewable = queue.iloc[0:0]

print(f"{len(queue)} pending, {len(reviewable)} with a proposal to review")
print()
print(
    store.to_rows(
        reviewable.head(20),
        ["incident_id", "detected_at", "proposal_target", "proposal_summary"],
    )
)


# ============================================================================
# CELL 3 — Read one in full
# ============================================================================

# Set this to an incident_id from the list above. Leaving the placeholder in
# place raises rather than writing a row for an incident that does not exist —
# which it did once, permanently, in an append-only store (D-043).
INCIDENT = "INC-xxxxxxxxxx"

row = store.load().set_index("incident_id").loc[INCIDENT]

print(f"{INCIDENT}  [{row.get('severity')}]  {row.get('rule')}")
print("-" * 70)
print(f"WHAT HAPPENED\n  {row.get('summary')}")
print()
print(f"DIAGNOSIS  ({row.get('diagnosis_confidence')}, {row.get('diagnosis_source')})")
print(f"  cause:  {row.get('diagnosis_cause')}")
print(f"  because: {row.get('diagnosis_reasoning')}")
print(f"  cites:  {row.get('diagnosis_cited_json')}")
print()
print(f"PROPOSAL  ({row.get('proposal_confidence')}, target {row.get('proposal_target')})")
print(f"  {row.get('proposal_summary')}")
print(f"  change:   {row.get('proposal_change')}")
print(f"  because:  {row.get('proposal_rationale')}")
print(f"  prevents: {row.get('proposal_prevents')}")
print(f"  risks:    {row.get('proposal_risks')}")
print(f"  cites:    {row.get('proposal_cited_json')}")
print()
print("MEASURED")
print(f"  {row.get('measured_json')}")


# ============================================================================
# CELL 4 — Decide (the proposed fix)
# ============================================================================

# approved  — you would merge this change unmodified
# rejected  — you would not
# pending   — you have not decided; leave it in the queue
#
# "Would you merge this as written" is the question, not "is this roughly
# right". A fix you would edit before applying counts as rejected and the note
# says what you would have changed — that distinction is what makes the
# acceptance figure mean anything.

DECISION = "pending"
NOTE = ""

store.decide(INCIDENT, DECISION, decided_by="michael", note=NOTE)

print(f"{INCIDENT}: {DECISION}")
print()
print("store:", store.summarise())


# ============================================================================
# CELL 5 — The score
# ============================================================================
#
# Both halves are named explicitly on purpose. The defaults are wrong by
# design: Seal() points at a relative path, the storage backend starts local,
# and the harness refuses to print anything rather than score against an empty
# directory (D-045). The two provenance lines above the table say which seal
# and which store produced the numbers under them.

from common import storage
from estate.faults.base import Seal
from eval import harness

storage.use_lakehouse(spark, prefix="tel_")
SEAL = Seal("/lakehouse/default/Files/sealed")   # lh_nightshift

print(harness.table(seal=SEAL))
print()
for key, value in harness.results(seal=SEAL).items():
    print(f"  {key:<30} {value}")


# ============================================================================
# CELL 6 — The judging queue
# ============================================================================
#
# Scoring a diagnosis is a human judgement, not a string comparison: "the
# source file contained no data rows" and the sealed "the landing file was
# written with its header row and no data rows" are the same answer in
# different words, and a keyword match would score this project's vocabulary
# rather than its correctness. This prints the sealed mechanism once, then
# every diagnosed incident beside it. Compare by eye, record in Cell 7.

from estate.faults.base import Seal
from eval import harness

SEAL = Seal("/lakehouse/default/Files/sealed")

frame = store.load()

# Which incidents belong to which sealed fault, so a diagnosis is scored
# against the right ground truth — and so one belonging to no sealed fault is
# flagged rather than judged against someone else's mechanism.
belongs = {
    incident_id: fault_id
    for fault_id, ids in harness.match(frame, SEAL).items()
    for incident_id in ids
}

if "diagnosis_cause" not in frame.columns:
    raise RuntimeError("no diagnoses in the store yet — run 30_diagnose first")

diagnosed = frame[frame["diagnosis_cause"].notna()]

for fault_id in sorted(set(belongs.values())):
    truth = SEAL.load(fault_id)
    print(f"SEALED MECHANISM — {fault_id}")
    print(f"  what was done:    {truth.mechanism}")
    print(f"  expected symptom: {truth.expected_symptom}")

    # A corrected record says so, with the superseded value and the evidence
    # (D-046). Printed here because anyone scoring against this record is
    # entitled to see that it has been changed and why.
    for correction in truth.corrections:
        print(f"  CORRECTED {correction['field']}: "
              f"{correction['from']} -> {correction['to']}")
        print(f"    because {correction['reason']}")
    print()

print(f"{len(diagnosed)} diagnosed incident(s)")
print("=" * 70)

for _, row in diagnosed.iterrows():
    # An incident outside every sealed window has no ground truth to be scored
    # against. Judging one for correctness would be scoring an opinion against
    # another opinion, so it is labelled rather than silently included. It can
    # still be judged `restatement` — "names no mechanism" is checkable from
    # the text alone — and the harness reports those separately (D-047).
    origin = belongs.get(row["incident_id"], "NO SEALED FAULT — do not judge")

    print(f"{row['incident_id']}  [{row.get('diagnosis_source')}]  {origin}")
    print(f"  cause:   {row.get('diagnosis_cause')}")
    print(f"  because: {row.get('diagnosis_reasoning')}")
    print(f"  verdict: {row.get('verdict') or 'not yet judged'}")
    print()


# ============================================================================
# CELL 7 — Record a verdict (the diagnosis)
# ============================================================================
#
# correct      the sealed mechanism, in whatever words
# partial      part of it, or the right mechanism with a wrong detail attached
# restatement  the symptom restated as the cause, naming no mechanism at all
# incorrect    a different cause
# abstained    declined to name a cause — a result, not a wrong answer
#
# Judge the diagnosis only. Whether the proposed fix is worth applying is a
# separate ruling and belongs in Cell 4.
#
# An abstention is recorded as "abstained" rather than "incorrect": a system
# that says "I don't know" when it doesn't know is behaving correctly, and
# scoring that as failure would reward confident guessing — which is exactly
# what `restatement` exists to catch (D-044).
#
# Read the reasoning as well as the cause before deciding. Five diagnoses were
# nearly scored "partial" on the grounds that they omitted a detail which was
# in fact stated in every one of their reasoning fields.

VERDICT_FOR = "INC-xxxxxxxxxx"
VERDICT = "abstained"
VERDICT_NOTE = ""

store.judge(VERDICT_FOR, VERDICT, "michael", VERDICT_NOTE)

print(f"{VERDICT_FOR}: {VERDICT}")
print()
print("store:", store.summarise())
