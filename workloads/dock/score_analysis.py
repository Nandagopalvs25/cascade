from dataclasses import dataclass, field

LIGAND_EFFICIENCY_SIZE_EXPONENT = 0.3
DEFAULT_SCORING_FUNCTION_ERROR_KCAL_PER_MOL = 2.0
SIZE_DRIVEN_RANKING_CORRELATION = -0.7
MINIMUM_COMPOUNDS_FOR_CORRELATION = 3

SCORE_ANALYSIS_CAVEAT = (
    "Vina affinities are empirical docking scores, not measured binding free energies. "
    "Reported RMSE against experiment ranges from roughly 0.65 to 5.48 kcal/mol depending on "
    "target and parameters, so differences smaller than scoring_function_error_kcal_per_mol "
    "carry no ranking information. Ligand efficiency and size-independent ligand efficiency are "
    "reported as context for the heavy-atom bias in the raw score; neither is a corrected "
    "potency estimate and neither should be used on its own to select compounds."
)


@dataclass
class ScoredCompound:
    name: str
    best_affinity: float
    heavy_atom_count: int


@dataclass
class ScoreAnalysis:
    compound_count: int
    scoring_function_error_kcal_per_mol: float
    best_affinity_kcal_per_mol: float
    worst_affinity_kcal_per_mol: float
    affinity_span_kcal_per_mol: float
    compounds_indistinguishable_from_best: list[str] = field(default_factory=list)
    ranking_separates_best_compound: bool = False
    affinity_heavy_atom_correlation: float | None = None
    ranking_is_size_driven: bool = False
    best_by_affinity: str = ""
    best_by_ligand_efficiency: str = ""
    best_by_size_independent_ligand_efficiency: str = ""
    metrics_agree_on_best_compound: bool = False
    control_compound_name: str | None = None
    control_affinity_rank: int | None = None
    compounds_scoring_better_than_control: list[str] = field(default_factory=list)
    caveat: str = SCORE_ANALYSIS_CAVEAT


def ligand_efficiency(best_affinity: float, heavy_atom_count: int) -> float | None:
    if heavy_atom_count <= 0:
        return None
    return round(-best_affinity / heavy_atom_count, 4)


def size_independent_ligand_efficiency(best_affinity: float, heavy_atom_count: int) -> float | None:
    if heavy_atom_count <= 0:
        return None
    return round(-best_affinity / (heavy_atom_count**LIGAND_EFFICIENCY_SIZE_EXPONENT), 4)


def _tie_averaged_ranks(values: list[float]) -> list[float]:
    ordered = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(ordered):
        last_tied = position
        while (
            last_tied + 1 < len(ordered)
            and values[ordered[last_tied + 1]] == values[ordered[position]]
        ):
            last_tied += 1
        shared_rank = (position + last_tied) / 2 + 1
        for index in range(position, last_tied + 1):
            ranks[ordered[index]] = shared_rank
        position = last_tied + 1
    return ranks


def spearman_rank_correlation(first: list[float], second: list[float]) -> float | None:
    if len(first) != len(second) or len(first) < MINIMUM_COMPOUNDS_FOR_CORRELATION:
        return None
    first_ranks = _tie_averaged_ranks(first)
    second_ranks = _tie_averaged_ranks(second)
    count = len(first_ranks)
    first_mean = sum(first_ranks) / count
    second_mean = sum(second_ranks) / count
    covariance = sum(
        (a - first_mean) * (b - second_mean) for a, b in zip(first_ranks, second_ranks, strict=True)
    )
    first_spread = sum((a - first_mean) ** 2 for a in first_ranks)
    second_spread = sum((b - second_mean) ** 2 for b in second_ranks)
    if first_spread == 0 or second_spread == 0:
        return None
    return round(covariance / (first_spread * second_spread) ** 0.5, 4)


def analyze_docking_scores(
    compounds: list[ScoredCompound],
    control_compound_name: str | None = None,
    scoring_function_error_kcal_per_mol: float = DEFAULT_SCORING_FUNCTION_ERROR_KCAL_PER_MOL,
) -> ScoreAnalysis | None:
    if not compounds:
        return None

    ranked = sorted(compounds, key=lambda compound: compound.best_affinity)
    best = ranked[0]
    worst = ranked[-1]
    indistinguishable = [
        compound.name
        for compound in ranked
        if compound.best_affinity - best.best_affinity <= scoring_function_error_kcal_per_mol
    ]

    with_efficiency = [
        (compound, ligand_efficiency(compound.best_affinity, compound.heavy_atom_count))
        for compound in ranked
    ]
    measurable = [(compound, value) for compound, value in with_efficiency if value is not None]
    best_by_efficiency = max(measurable, key=lambda pair: pair[1])[0].name if measurable else ""
    best_by_size_independent = (
        max(
            ranked,
            key=lambda compound: (
                size_independent_ligand_efficiency(
                    compound.best_affinity, compound.heavy_atom_count
                )
                or float("-inf")
            ),
        ).name
        if measurable
        else ""
    )

    correlation = spearman_rank_correlation(
        [float(compound.heavy_atom_count) for compound in ranked],
        [compound.best_affinity for compound in ranked],
    )

    analysis = ScoreAnalysis(
        compound_count=len(ranked),
        scoring_function_error_kcal_per_mol=scoring_function_error_kcal_per_mol,
        best_affinity_kcal_per_mol=best.best_affinity,
        worst_affinity_kcal_per_mol=worst.best_affinity,
        affinity_span_kcal_per_mol=round(worst.best_affinity - best.best_affinity, 3),
        compounds_indistinguishable_from_best=indistinguishable,
        ranking_separates_best_compound=len(indistinguishable) == 1,
        affinity_heavy_atom_correlation=correlation,
        ranking_is_size_driven=(
            correlation is not None and correlation <= SIZE_DRIVEN_RANKING_CORRELATION
        ),
        best_by_affinity=best.name,
        best_by_ligand_efficiency=best_by_efficiency,
        best_by_size_independent_ligand_efficiency=best_by_size_independent,
    )
    analysis.metrics_agree_on_best_compound = (
        analysis.best_by_affinity
        == analysis.best_by_ligand_efficiency
        == analysis.best_by_size_independent_ligand_efficiency
        != ""
    )

    if control_compound_name:
        lowered = control_compound_name.strip().lower()
        for rank, compound in enumerate(ranked, start=1):
            if compound.name.strip().lower() == lowered:
                analysis.control_compound_name = compound.name
                analysis.control_affinity_rank = rank
                analysis.compounds_scoring_better_than_control = [
                    other.name for other in ranked[: rank - 1]
                ]
                break

    return analysis
