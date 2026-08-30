import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from protenix_job import STRUCTURE_SUFFIX, SUMMARY_CONFIDENCE_MARKER

SAMPLE_RANK_PATTERN = re.compile(rf"{re.escape(SUMMARY_CONFIDENCE_MARKER)}(\d+)\.json$")
SEED_DIRECTORY_PATTERN = re.compile(r"^seed_(\d+)$")

RANKING_SCORE_KEY = "ranking_score"
SCALAR_CONFIDENCE_KEYS = (
    "ranking_score",
    "plddt",
    "gpde",
    "ptm",
    "iptm",
    "has_clash",
    "chain_plddt",
    "protein_chain_plddt",
    "ligand_chain_plddt",
)


@dataclass
class ComplexPrediction:
    complex_name: str
    compound_name: str
    seed: int | None
    sample_rank: int
    ranking_score: float | None
    metrics: dict = field(default_factory=dict)
    structure_path: Path | None = None


def _seed_from_path(path: Path) -> int | None:
    for part in path.parts:
        match = SEED_DIRECTORY_PATTERN.match(part)
        if match:
            return int(match.group(1))
    return None


def _sample_rank_from_file_name(name: str) -> int:
    match = SAMPLE_RANK_PATTERN.search(name)
    return int(match.group(1)) if match else 0


def _complex_name_from_file_name(name: str) -> str:
    return name.split(SUMMARY_CONFIDENCE_MARKER)[0]


def _scalar_metrics(payload: dict) -> dict:
    metrics = {}
    for key in SCALAR_CONFIDENCE_KEYS:
        if key in payload and isinstance(payload[key], (int, float, bool)):
            metrics[key] = payload[key]
    return metrics


def _matching_structure_path(
    summary_path: Path, complex_name: str, sample_rank: int
) -> Path | None:
    candidate = summary_path.parent / f"{complex_name}_sample_{sample_rank}{STRUCTURE_SUFFIX}"
    if candidate.exists():
        return candidate
    matches = sorted(summary_path.parent.glob(f"{complex_name}_sample_*{STRUCTURE_SUFFIX}"))
    return matches[0] if matches else None


def load_complex_predictions(
    summary_paths: list[Path], compound_name_by_complex: dict[str, str]
) -> list[ComplexPrediction]:
    predictions: list[ComplexPrediction] = []
    for summary_path in summary_paths:
        payload = json.loads(summary_path.read_text())
        complex_name = _complex_name_from_file_name(summary_path.name)
        sample_rank = _sample_rank_from_file_name(summary_path.name)
        ranking_score = payload.get(RANKING_SCORE_KEY)
        predictions.append(
            ComplexPrediction(
                complex_name=complex_name,
                compound_name=compound_name_by_complex.get(complex_name, complex_name),
                seed=_seed_from_path(summary_path),
                sample_rank=sample_rank,
                ranking_score=(
                    float(ranking_score) if isinstance(ranking_score, (int, float)) else None
                ),
                metrics=_scalar_metrics(payload),
                structure_path=_matching_structure_path(summary_path, complex_name, sample_rank),
            )
        )
    return predictions


def best_prediction_per_compound(
    predictions: list[ComplexPrediction],
) -> list[ComplexPrediction]:
    best: dict[str, ComplexPrediction] = {}
    for prediction in predictions:
        current = best.get(prediction.compound_name)
        if current is None:
            best[prediction.compound_name] = prediction
            continue
        if _is_better(prediction, current):
            best[prediction.compound_name] = prediction
    return rank_predictions_by_confidence(list(best.values()))


def _is_better(candidate: ComplexPrediction, incumbent: ComplexPrediction) -> bool:
    if candidate.ranking_score is None:
        return False
    if incumbent.ranking_score is None:
        return True
    if candidate.ranking_score != incumbent.ranking_score:
        return candidate.ranking_score > incumbent.ranking_score
    return candidate.sample_rank < incumbent.sample_rank


def rank_predictions_by_confidence(
    predictions: list[ComplexPrediction],
) -> list[ComplexPrediction]:
    return sorted(
        predictions,
        key=lambda prediction: (
            prediction.ranking_score is None,
            -(prediction.ranking_score or 0.0),
            prediction.compound_name,
        ),
    )
