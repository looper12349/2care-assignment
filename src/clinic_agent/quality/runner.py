"""Before/after evaluation uses LangSmith's real SDK, including offline runs."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from langsmith import Client, tracing_context
from langsmith.evaluation import evaluate
from langsmith.schemas import Example

from clinic_agent.quality.evaluators import CRITICAL_METRICS, EVALUATOR_VERSION, world_and_audit
from clinic_agent.quality.improvements import (
    acceptance_gates,
    diagnose_failures,
    propose_improvement,
)
from clinic_agent.quality.report import write_html_report
from clinic_agent.quality.scenario_driver import load_scenarios, run_scenario


def _examples(
    catalog: dict, version: str, model_name: str | None, online: bool = False
) -> list[Example]:
    dataset_id = uuid5(NAMESPACE_URL, f"clinic-synthetic:{catalog['version']}")
    examples = []
    # Expected outcomes only go to reference outputs. The patient driver and
    # interpreter cannot peek at them when deciding what to do.
    fixture_keys = {
        "id",
        "title",
        "steps",
        "max_turns",
        "faults",
        "stop_after_script",
        "probe_other_principal",
        "replay_last_turn",
    }
    for scenario in catalog["scenarios"]:
        inputs = {
            "scenario": {k: v for k, v in scenario.items() if k in fixture_keys},
            "settings": {
                "behavior_version": version,
                "trace_online": online,
                "model_name": model_name,
                "fixed_now": catalog["fixed_now"],
                "timezone": catalog["timezone"],
                "patient_id": catalog["patient_id"],
                "tenant_id": catalog["tenant_id"],
            },
        }
        examples.append(
            Example(
                id=uuid5(dataset_id, scenario["id"]),
                dataset_id=dataset_id,
                inputs=inputs,
                outputs={"scenario": scenario},
                created_at=datetime.now(UTC),
                metadata={
                    "scenario_id": scenario["id"],
                    "split": scenario["split"],
                    "synthetic": True,
                },
            )
        )
    return examples


def _summarize(rows: list[dict], version: str, expected_runs: int) -> dict:
    keys = sorted({key for row in rows for key in row["scores"]})
    metrics = {
        key: {"passed": sum(row["scores"].get(key) == 1 for row in rows), "total": expected_runs}
        for key in keys
    }
    for value in metrics.values():
        value["rate"] = value["passed"] / expected_runs if expected_runs else 0.0
    passed = sum(
        bool(row["scores"])
        and all(
            value == 1 for key, value in row["scores"].items() if key != "communication_quality"
        )
        and not row.get("error")
        for row in rows
    )
    resolution_passed = sum(row["scores"].get("task_resolution") == 1 for row in rows)
    critical = sum(row["scores"].get(key) != 1 for row in rows for key in CRITICAL_METRICS)
    # Missing runs are failures in the declared denominator, never discarded.
    result = {
        "version": version,
        "passed": passed,
        "total": expected_runs,
        "resolution_passed": resolution_passed,
        "resolution_rate": resolution_passed / expected_runs if expected_runs else 0.0,
        "critical_violations": critical,
        "missing_runs": max(expected_runs - len(rows), 0),
        "rows": rows,
        "summary": metrics,
    }
    result["metrics"] = {
        key: result[key] for key in ("passed", "total", "resolution_rate", "critical_violations")
    }
    return result


def _run_experiment(
    catalog: dict,
    version: str,
    repetitions: int,
    online: bool,
    model_name: str | None,
    client: Client,
) -> dict:
    examples = _examples(catalog, version, model_name, online)
    if online:
        # Remote datasets contain synthetic fixtures only and require an explicit
        # online flag. Nothing is made public by this private experiment upload.
        name = f"clinic-synthetic-{catalog['version']}"
        if client.has_dataset(dataset_name=name):
            dataset = client.read_dataset(dataset_name=name)
        else:
            dataset = client.create_dataset(
                dataset_name=name, description="Synthetic clinic conversations; no patient records."
            )
            client.create_examples(
                dataset_id=dataset.id,
                examples=[
                    {
                        "id": example.id,
                        "inputs": {
                            **example.inputs,
                            "settings": {
                                k: v
                                for k, v in example.inputs["settings"].items()
                                if k != "behavior_version"
                            },
                        },
                        "outputs": example.outputs,
                        "metadata": example.metadata,
                    }
                    for example in examples
                ],
            )
        # Dataset examples remain stable across versions. The version enters
        # through target configuration so both experiments share the dataset.
        data: Any = name

        def target(inputs: dict) -> dict:
            configured = {
                **inputs,
                "settings": {
                    **inputs["settings"],
                    "behavior_version": version,
                    "model_name": model_name,
                    "trace_online": True,
                },
            }
            return run_scenario(configured)
    else:
        data = examples
        target = run_scenario
    with tracing_context(enabled=online):
        experiment = evaluate(
            target,
            data=data,
            evaluators=[world_and_audit],
            experiment_prefix=f"clinic-{version}",
            metadata={
                "behavior_version": version,
                "dataset_version": catalog["version"],
                "evaluator_version": EVALUATOR_VERSION,
                "synthetic": True,
                "actor": "langchain-model" if model_name else "deterministic-demo-interpreter",
            },
            client=client,
            num_repetitions=repetitions,
            max_concurrency=0,
            upload_results=online,
            error_handling="log",
        )
        rows = []
        repetitions_seen: dict[str, int] = {}
        for item in experiment:
            run = item["run"]
            example = item["example"]
            outputs = run.outputs or {}
            expected = (example.outputs or {}).get("scenario", {})
            scenario_id = outputs.get("scenario_id") or expected.get("id", "unknown")
            repetitions_seen[scenario_id] = repetitions_seen.get(scenario_id, 0) + 1
            feedback = item.get("evaluation_results", {}).get("results", [])
            scores = {feedback_item.key: feedback_item.score for feedback_item in feedback}
            evidence = {feedback_item.key: feedback_item.comment for feedback_item in feedback}
            errors = [
                f"{feedback_item.key}: evaluator returned no score"
                for feedback_item in feedback
                if feedback_item.score is None
            ]
            error = outputs.get("error") or run.error or ("; ".join(errors) if errors else None)
            rows.append(
                {
                    "scenario_id": scenario_id,
                    "name": expected.get("title", scenario_id),
                    "split": expected.get("split", "core"),
                    "expected": expected.get("expected"),
                    "repetition": repetitions_seen[scenario_id],
                    "run_id": str(run.id),
                    "final_status": outputs.get("final_response", {}).get("status"),
                    "scores": scores,
                    "evidence": evidence,
                    "transcript": outputs.get("transcript", []),
                    "world": outputs.get("world", {}),
                    "driver_events": outputs.get("driver_events", []),
                    "error": error,
                }
            )
    report = _summarize(rows, version, len(examples) * repetitions)
    report["experiment_name"] = getattr(experiment, "experiment_name", None)
    return report


def run_eval_loop(
    output_dir: str | Path,
    repetitions: int = 1,
    online: bool = False,
    interpreter: str = "demo",
    model_name: str | None = None,
) -> dict[str, Any]:
    """Run actual baseline, derive a bounded artifact, rerun, and apply gates.

    Returns a complete report and writes local JSON/HTML. Acceptance does not
    activate patient behavior; the conversation service owns explicit activation.
    """
    if repetitions not in (1, 3):
        raise ValueError("Repetitions must be 1 or 3.")
    if interpreter not in ("demo", "live"):
        raise ValueError("Interpreter must be demo or live.")
    if interpreter == "live" and not (os.environ.get("OPENAI_API_KEY") and model_name):
        raise ValueError("Live evaluation requires OPENAI_API_KEY and an explicit model name.")
    if online and not (os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY")):
        raise ValueError(
            "Online evaluation requires a LangSmith API key and an explicit online request."
        )
    selected_model = model_name if interpreter == "live" else None
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    catalog = load_scenarios()
    # A closed local endpoint and supplied info prevent accidental SDK metadata
    # requests in offline mode even if unrelated tracing environment vars exist.
    client = (
        Client()
        if online
        else Client(
            api_url="http://127.0.0.1:1",
            api_key="local-only-not-a-real-key",
            auto_batch_tracing=False,
            info={"version": "offline"},
        )
    )
    try:
        baseline = _run_experiment(catalog, "v1", repetitions, online, selected_model, client)
        failures = diagnose_failures(baseline["rows"])
        try:
            artifact = propose_improvement(failures)
        except ValueError as exc:
            artifact = None
            candidate = _summarize([], "v2", len(catalog["scenarios"]) * repetitions)
            gates = {
                "accepted": False,
                "checks": [{"name": "concrete_safe_failure", "passed": False, "detail": str(exc)}],
                "regressions": [],
                "promotion": "baseline-retained",
            }
        else:
            # The allowlisted candidate is applied to a fresh isolated world.
            # Every original/held-out scenario is rerun using the same reference.
            candidate = _run_experiment(
                catalog, artifact.candidate_version, repetitions, online, selected_model, client
            )
            gates = acceptance_gates(
                baseline, candidate, artifact, len(catalog["scenarios"]) * repetitions
            )
            artifact.status = "accepted" if gates["accepted"] else "rejected"
        candidate["accepted"] = gates["accepted"]
        scenarios = []
        for scenario in catalog["scenarios"]:
            old = [r for r in baseline["rows"] if r["scenario_id"] == scenario["id"]]
            new = [r for r in candidate["rows"] if r["scenario_id"] == scenario["id"]]
            old_count = sum(r["scores"].get("task_resolution") == 1 for r in old)
            new_count = sum(r["scores"].get("task_resolution") == 1 for r in new)
            scenarios.append(
                {
                    "id": scenario["id"],
                    "name": scenario["title"],
                    "split": scenario["split"],
                    "baseline": old_count == repetitions,
                    "candidate": new_count == repetitions,
                    "baseline_passed": old_count,
                    "candidate_passed": new_count,
                    "total": repetitions,
                    "detail": next(
                        (
                            r["evidence"].get("task_resolution", "")
                            for r in new
                            if r["scores"].get("task_resolution") != 1
                        ),
                        "All candidate repetitions resolved correctly."
                        if new
                        else "Candidate not evaluated.",
                    ),
                }
            )
        artifact_path = directory / "improvement.json"
        report = {
            "schema_version": "1.0",
            "generated_at": datetime.now(UTC).isoformat(),
            "engine": {
                "platform": "LangSmith SDK",
                "upload_results": online,
                "actor": "langchain-model" if selected_model else "deterministic-demo-interpreter",
                "actor_label": "LangChain structured model"
                if selected_model
                else "Deterministic demo interpreter",
                "model_name": selected_model,
                "judge": "independent persisted-world and event-order code evaluators",
                "evaluator_version": EVALUATOR_VERSION,
            },
            "dataset": {
                "version": catalog["version"],
                "count": len(catalog["scenarios"]),
                "core_count": sum(s["split"] == "core" for s in catalog["scenarios"]),
                "heldout_count": sum(s["split"] == "heldout" for s in catalog["scenarios"]),
                "repetitions": repetitions,
                "fixed_now": catalog["fixed_now"],
            },
            "baseline": baseline,
            "candidate": candidate,
            "accepted": gates["accepted"],
            "gates": gates,
            "improvement": artifact.model_dump() if artifact else None,
            "failures": failures,
            "scenarios": scenarios,
            "limitations": [
                "Demo mode uses a bounded deterministic language interpreter; it does not establish real LLM reliability.",
                "Synthetic fixtures exercise explicit configured emergency phrases; they do not validate clinical triage.",
                "One or three repetitions are demonstration evidence, not production assurance.",
                "The candidate activates an existing reviewed recovery branch. It does not rewrite code, retrain model weights, or weaken safety rules.",
                "Communication quality needs a separately calibrated optional judge and human review; deterministic scores verify behavior and side effects.",
            ],
            "note": "Private synthetic LangSmith experiment upload explicitly enabled."
            if online
            else "Evaluated with the LangSmith SDK locally. No dataset, traces, or results were uploaded.",
            "files": {
                "report_json": str(directory / "report.json"),
                "report_html": str(directory / "report.html"),
                "artifact_json": str(artifact_path) if artifact else None,
            },
        }
        if artifact:
            artifact_path.write_text(json.dumps(artifact.model_dump(), indent=2) + "\n")
        (directory / "failures.json").write_text(json.dumps(failures, indent=2) + "\n")
        (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        write_html_report(report, directory / "report.html")
        return report
    finally:
        client.close()
