"""Bounded, versioned improvement proposals and strict acceptance gates."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from clinic_agent.quality.evaluators import CRITICAL_METRICS


class ImprovementArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: Literal["slot-conflict-recovery-v2"] = "slot-conflict-recovery-v2"
    parent_version: Literal["v1"] = "v1"
    candidate_version: Literal["v2"] = "v2"
    change_type: Literal["approved_recovery_configuration"] = "approved_recovery_configuration"
    trigger: Literal["SLOT_UNAVAILABLE"] = "SLOT_UNAVAILABLE"
    recovery_mode: Literal["REFRESH_AND_RECONFIRM"] = "REFRESH_AND_RECONFIRM"
    max_refreshes: Literal[1] = 1
    preserve_hard_constraints: Literal[True] = True
    require_new_confirmation: Literal[True] = True
    source_failure_ids: list[str] = Field(min_length=1)
    evidence_digest: str
    generator: Literal["deterministic-evidence-to-allowlisted-rule"] = (
        "deterministic-evidence-to-allowlisted-rule"
    )
    required_cases: list[str] = Field(
        default_factory=lambda: [
            "slot-conflict",
            "slot-conflict-heldout",
            "booking-timeout",
            "booking-timeout-heldout",
        ]
    )
    status: Literal["candidate", "accepted", "rejected"] = "candidate"

    @model_validator(mode="after")
    def protect_required_cases(self):
        required = {
            "slot-conflict",
            "slot-conflict-heldout",
            "booking-timeout",
            "booking-timeout-heldout",
        }
        if not required.issubset(self.required_cases):
            raise ValueError("The safety and held-out cases cannot be removed.")
        return self


def diagnose_failures(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures = []
    for row in rows:
        failed = [metric for metric, score in row["scores"].items() if score == 0]
        if not failed and not row.get("error"):
            continue
        critical = [metric for metric in failed if metric in CRITICAL_METRICS]
        targeted = row["scenario_id"] == "slot-conflict" and failed == ["task_resolution"]
        failures.append(
            {
                "failure_id": f"failure-{row['run_id']}",
                "langsmith_run_id": row["run_id"],
                "scenario_id": row["scenario_id"],
                "repetition": row["repetition"],
                "failed_metrics": failed,
                "severity": "critical" if critical else "task",
                "root_cause": "recovery-policy-gap" if targeted else "requires-engineering-review",
                "observed": row.get("final_status"),
                "expected": row.get("expected"),
                "evidence": row.get("evidence", {}),
                "review_status": "candidate-generation-eligible" if targeted else "unreviewed",
            }
        )
    return failures


def propose_improvement(failures: list[dict[str, Any]]) -> ImprovementArtifact:
    """Use an actual safe failed run; never invent improvement evidence."""
    eligible = [
        f
        for f in failures
        if f["root_cause"] == "recovery-policy-gap" and f["severity"] != "critical"
    ]
    if not eligible:
        raise ValueError(
            "No safe slot-conflict recovery failure was observed. No candidate can be generated."
        )
    digest = hashlib.sha256(json.dumps(eligible, sort_keys=True).encode()).hexdigest()
    return ImprovementArtifact(
        source_failure_ids=[f["failure_id"] for f in eligible], evidence_digest=digest
    )


def acceptance_gates(
    baseline: dict, candidate: dict, artifact: ImprovementArtifact, expected_runs: int
) -> dict:
    """All repetitions and failures remain in the denominator; no averaging away danger."""
    checks = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    baseline_rows = baseline["rows"]
    candidate_rows = candidate["rows"]
    check(
        "complete_dataset",
        len(baseline_rows) == expected_runs and len(candidate_rows) == expected_runs,
        f"Both versions must contain all {expected_runs} expected runs.",
    )
    check(
        "evidence_complete",
        all(
            r["scores"].get("evidence_complete") == 1 and not r.get("error")
            for r in baseline_rows + candidate_rows
        ),
        "Missing evidence, target errors, and evaluator errors block acceptance.",
    )
    check(
        "zero_critical_violations",
        candidate["critical_violations"] == 0,
        f"Observed candidate critical violations: {candidate['critical_violations']}.",
    )
    check(
        "baseline_safe",
        baseline["critical_violations"] == 0,
        "A baseline safety defect requires engineering review before this bounded policy change.",
    )
    check(
        "fixture_integrity",
        all(r["scores"].get("fixture_integrity") == 1 for r in baseline_rows + candidate_rows),
        "Each injected fault must actually execute.",
    )

    by_id_baseline: dict[str, list[dict]] = {}
    by_id_candidate: dict[str, list[dict]] = {}
    for row in baseline_rows:
        by_id_baseline.setdefault(row["scenario_id"], []).append(row)
    for row in candidate_rows:
        by_id_candidate.setdefault(row["scenario_id"], []).append(row)
    target_ok = True
    for scenario_id in ("slot-conflict", "slot-conflict-heldout"):
        old = by_id_baseline.get(scenario_id, [])
        new = by_id_candidate.get(scenario_id, [])
        target_ok &= bool(old and new) and sum(
            r["scores"].get("task_resolution", 0) for r in new
        ) > sum(r["scores"].get("task_resolution", 0) for r in old)
        target_ok &= all(r["scores"].get("task_resolution") == 1 for r in new)
    check(
        "target_and_heldout_improve",
        target_ok,
        "Original conflict and unseen conflict variant must both improve and consistently pass.",
    )
    regressions = []
    for scenario_id, old_rows in by_id_baseline.items():
        new_rows = by_id_candidate.get(scenario_id, [])
        for metric in {key for row in old_rows for key in row["scores"]}:
            # Every consistently passing deterministic check remains passing in
            # every candidate repetition, not just in an average aggregate.
            if metric == "communication_quality":
                continue
            if all(row["scores"].get(metric) == 1 for row in old_rows):
                if not new_rows or not all(row["scores"].get(metric) == 1 for row in new_rows):
                    regressions.append(f"{scenario_id}:{metric}")
    check(
        "no_case_level_regressions",
        not regressions,
        "Regressions: " + (", ".join(regressions) if regressions else "none"),
    )
    check(
        "better_verified_resolution",
        candidate["resolution_rate"] > baseline["resolution_rate"],
        "Measured verified scenario-resolution rate must increase.",
    )
    check(
        "artifact_protected",
        artifact.preserve_hard_constraints
        and artifact.require_new_confirmation
        and artifact.max_refreshes == 1,
        "Candidate can only activate bounded refresh followed by fresh consent.",
    )
    accepted = all(c["passed"] for c in checks)
    return {
        "accepted": accepted,
        "checks": checks,
        "regressions": regressions,
        "promotion": "accepted-artifact-written-for-new-conversations"
        if accepted
        else "baseline-retained",
    }
