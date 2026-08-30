import csv
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from assessment import (
    VERDICT_FAIL,
    VERDICT_FLAG,
    VERDICT_PASS,
    CompoundAssessment,
    predicted_blood_brain_barrier_penetration,
)
from compounds import CompoundFailure
from job_spec import AdmetParams, JobSpec, TargetStructure

ASSESSMENTS_FILE_NAME = "admet.csv"
RESULTS_FILE_NAME = "results.json"
RESULTS_ARCHIVE_NAME = "results.zip"

VERDICT_SORT_ORDER = {VERDICT_PASS: 0, VERDICT_FLAG: 1, VERDICT_FAIL: 2}


@dataclass
class ControlCompoundSummary:
    requested_name: str | None = None
    status: str = "not_requested"
    verdict: str | None = None
    detail: str = ""
    reasons: list[str] | None = None


def rank_assessments_by_promotability(
    assessments: list[CompoundAssessment],
) -> list[CompoundAssessment]:
    return sorted(
        assessments,
        key=lambda assessment: (
            VERDICT_SORT_ORDER[assessment.verdict],
            -assessment.properties.drug_likeness_qed,
        ),
    )


def verdict_counts(assessments: list[CompoundAssessment]) -> dict[str, int]:
    counts = {VERDICT_PASS: 0, VERDICT_FLAG: 0, VERDICT_FAIL: 0}
    for assessment in assessments:
        counts[assessment.verdict] += 1
    return counts


def liability_counts(assessments: list[CompoundAssessment]) -> dict[str, int]:
    counts = {"pains": 0, "brenk": 0, "nih": 0, "herg_high": 0, "herg_moderate": 0}
    for assessment in assessments:
        for hit in assessment.alerts:
            if hit.count:
                counts[hit.catalog] += 1
        if assessment.herg.band == "high":
            counts["herg_high"] += 1
        elif assessment.herg.band == "moderate":
            counts["herg_moderate"] += 1
    return counts


def measure_control_compound(
    spec: JobSpec, assessments: list[CompoundAssessment]
) -> ControlCompoundSummary:
    if spec.control_compound is None:
        return ControlCompoundSummary()

    requested = spec.control_compound.strip()
    lowered = requested.lower()
    match = next(
        (assessment for assessment in assessments if assessment.name.strip().lower() == lowered),
        None,
    )
    if match is None:
        return ControlCompoundSummary(
            requested_name=requested,
            status="not_in_library",
            detail="no assessed compound carries the control compound name",
        )

    if match.verdict == VERDICT_FAIL:
        return ControlCompoundSummary(
            requested_name=requested,
            status="failed_unexpectedly",
            verdict=match.verdict,
            detail=(
                "the control is a known drug, so failing it means these filters are "
                "miscalibrated for this chemical series"
            ),
            reasons=match.reasons,
        )

    return ControlCompoundSummary(
        requested_name=requested,
        status="measured",
        verdict=match.verdict,
        detail=f"control assessed as {match.verdict}",
        reasons=match.reasons,
    )


def write_assessments_csv(assessments: list[CompoundAssessment], directory: Path) -> Path:
    destination = directory / ASSESSMENTS_FILE_NAME
    with destination.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "rank",
                "compound_id",
                "verdict",
                "molecular_weight",
                "crippen_logp",
                "tpsa",
                "hbd",
                "hba",
                "rotatable_bonds",
                "drug_likeness_qed",
                "synthetic_accessibility",
                "lipinski_violations",
                "pains_alerts",
                "brenk_alerts",
                "nih_alerts",
                "herg_risk",
                "blood_brain_barrier",
                "reasons",
            ]
        )
        for rank, assessment in enumerate(rank_assessments_by_promotability(assessments), start=1):
            properties = assessment.properties
            counts = {hit.catalog: hit.count for hit in assessment.alerts}
            writer.writerow(
                [
                    rank,
                    assessment.name,
                    assessment.verdict,
                    f"{properties.molecular_weight:.2f}",
                    f"{properties.crippen_logp:.3f}",
                    f"{properties.topological_polar_surface_area:.2f}",
                    properties.hydrogen_bond_donors,
                    properties.hydrogen_bond_acceptors,
                    properties.rotatable_bonds,
                    f"{properties.drug_likeness_qed:.3f}",
                    f"{properties.synthetic_accessibility:.2f}",
                    assessment.lipinski_violation_count,
                    counts.get("pains", 0),
                    counts.get("brenk", 0),
                    counts.get("nih", 0),
                    assessment.herg.band,
                    predicted_blood_brain_barrier_penetration(properties),
                    "; ".join(assessment.reasons),
                ]
            )
    return destination


def _assessment_as_dict(rank: int, assessment: CompoundAssessment) -> dict:
    return {
        "rank": rank,
        "compound_id": assessment.name,
        "smiles": assessment.smiles,
        "verdict": assessment.verdict,
        "reasons": assessment.reasons,
        "properties": assessment.properties.as_dict(),
        "blood_brain_barrier": predicted_blood_brain_barrier_penetration(assessment.properties),
        "structural_alerts": [
            {
                "catalog": hit.catalog,
                "count": hit.count,
                "descriptions": hit.descriptions,
            }
            for hit in assessment.alerts
        ],
        "herg": {
            "band": assessment.herg.band,
            "basic_amine_count": assessment.herg.basic_amine_count,
            "reason": assessment.herg.reason,
        },
        "rule_sets": [
            {
                "name": outcome.name,
                "passed": outcome.passed,
                "violations": outcome.violations,
            }
            for outcome in assessment.rule_sets
        ],
    }


def target_provenance(target: TargetStructure | None) -> dict | None:
    if target is None:
        return None
    return {
        "source": target.source,
        "reference": target.reference,
        "pdb_id": target.pdb_id,
    }


def build_run_summary(
    spec: JobSpec,
    params: AdmetParams,
    assessments: list[CompoundAssessment],
    failures: list[CompoundFailure],
    control: ControlCompoundSummary,
) -> dict:
    ranked = rank_assessments_by_promotability(assessments)
    return {
        "run_id": spec.run_id,
        "workload": spec.workload,
        "status": "succeeded",
        "method": {
            "engine": "rdkit",
            "basis": "physicochemical descriptors, published rule sets, structural alert catalogs",
            "caveat": (
                "these are deterministic rule-based and structural-alert predictions, "
                "not trained ADMET regression models"
            ),
        },
        "target": target_provenance(spec.target),
        "params": params.model_dump(),
        "compounds": {
            "assessed": len(assessments),
            "failed": len(failures),
            "failures": [
                {"compound_id": failure.name, "reason": failure.reason} for failure in failures
            ],
        },
        "verdict_counts": verdict_counts(assessments),
        "liability_counts": liability_counts(assessments),
        "control_compound": {
            "requested_name": control.requested_name,
            "status": control.status,
            "verdict": control.verdict,
            "detail": control.detail,
            "reasons": control.reasons,
        },
        "assessments": [
            _assessment_as_dict(rank, assessment) for rank, assessment in enumerate(ranked, start=1)
        ],
    }


def write_run_summary(summary: dict, directory: Path) -> Path:
    destination = directory / RESULTS_FILE_NAME
    destination.write_text(json.dumps(summary, indent=2))
    return destination


def write_results_archive(outputs_directory: Path, workspace: Path) -> Path:
    archive_base = workspace / RESULTS_ARCHIVE_NAME.removesuffix(".zip")
    return Path(shutil.make_archive(str(archive_base), "zip", root_dir=outputs_directory))
