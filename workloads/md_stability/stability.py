import math
from dataclasses import dataclass, field

STABILITY_CAVEAT = (
    "Short implicit-solvent molecular dynamics tests whether a docked pose persists, not how "
    "tightly the compound binds. Pose stability is not a binding free energy and must not be read "
    "as a predicted affinity in kcal/mol. A stable pose means the docking geometry survived "
    "dynamics; it does not rank compounds against one another by potency."
)

Coordinate = tuple[float, float, float]
ContactPair = tuple[int, int]

CONTACT_TAIL_FRACTION = 0.2


def squared_distance(first: Coordinate, second: Coordinate) -> float:
    return sum((a - b) ** 2 for a, b in zip(first, second, strict=True))


def heavy_atom_rmsd_in_receptor_frame(
    reference: list[Coordinate], frame: list[Coordinate]
) -> float:
    if not reference or len(reference) != len(frame):
        raise ValueError("pose frames must hold the same number of heavy atoms")
    total = sum(squared_distance(a, b) for a, b in zip(reference, frame, strict=True))
    return round(math.sqrt(total / len(reference)), 4)


def receptor_ligand_contacts(
    receptor: list[Coordinate], ligand: list[Coordinate], cutoff_angstrom: float
) -> set[ContactPair]:
    cutoff_squared = cutoff_angstrom**2
    return {
        (receptor_index, ligand_index)
        for receptor_index, receptor_atom in enumerate(receptor)
        for ligand_index, ligand_atom in enumerate(ligand)
        if squared_distance(receptor_atom, ligand_atom) <= cutoff_squared
    }


def retained_contact_fraction(
    initial: set[ContactPair],
    receptor: list[Coordinate],
    ligand: list[Coordinate],
    break_cutoff_angstrom: float,
) -> float:
    if not initial:
        return 0.0
    break_squared = break_cutoff_angstrom**2
    retained = sum(
        1
        for receptor_index, ligand_index in initial
        if squared_distance(receptor[receptor_index], ligand[ligand_index]) <= break_squared
    )
    return round(retained / len(initial), 4)


def sustained_tail_mean(values: list[float], tail_fraction: float) -> float:
    if not values:
        return 0.0
    window = max(1, round(len(values) * tail_fraction))
    tail = values[-window:]
    return round(sum(tail) / len(tail), 4)


@dataclass(frozen=True)
class PoseFailure:
    name: str
    reason: str


@dataclass
class PoseStabilityTrajectory:
    compound_id: str
    rmsd_series_angstrom: list[float] = field(default_factory=list)
    contact_retention_series: list[float] = field(default_factory=list)
    affinity_rank: int | None = None
    best_affinity_kcal_per_mol: float | None = None
    heavy_atoms: int | None = None

    def record_frame(self, rmsd_angstrom: float, retained_contacts: float) -> None:
        self.rmsd_series_angstrom.append(rmsd_angstrom)
        self.contact_retention_series.append(retained_contacts)


@dataclass(frozen=True)
class PoseStabilitySummary:
    compound_id: str
    verdict: str
    reasons: list[str]
    frames: int
    final_rmsd_angstrom: float
    mean_rmsd_angstrom: float
    maximum_rmsd_angstrom: float
    final_contact_retention: float
    mean_contact_retention: float
    sustained_contact_retention: float
    affinity_rank: int | None = None
    best_affinity_kcal_per_mol: float | None = None
    heavy_atoms: int | None = None


def mean_of(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def pose_left_the_pocket(
    final_contact_retention: float, contact_retention_threshold: float
) -> bool:
    return final_contact_retention < contact_retention_threshold


def pose_drifted(final_rmsd_angstrom: float, drift_threshold_angstrom: float) -> bool:
    return final_rmsd_angstrom > drift_threshold_angstrom


def stability_reasons(
    final_rmsd: float,
    maximum_rmsd: float,
    final_contacts: float,
    drift_threshold: float,
    contact_threshold: float,
) -> list[str]:
    reasons: list[str] = []
    if pose_left_the_pocket(final_contacts, contact_threshold):
        reasons.append(
            f"only {final_contacts:.0%} of the docked receptor contacts remained at the end of "
            f"the trajectory, below the {contact_threshold:.0%} threshold, so the compound left "
            f"the pocket it was docked into"
        )
    if pose_drifted(final_rmsd, drift_threshold):
        reasons.append(
            f"the ligand finished {final_rmsd} A from its docked pose, beyond the "
            f"{drift_threshold} A drift threshold"
        )
    if maximum_rmsd > drift_threshold and not pose_drifted(final_rmsd, drift_threshold):
        reasons.append(
            f"the pose excursed to {maximum_rmsd} A mid-trajectory before returning to "
            f"{final_rmsd} A, so it is mobile but not displaced"
        )
    if not reasons:
        reasons.append(
            f"the ligand stayed within {final_rmsd} A of its docked pose and retained "
            f"{final_contacts:.0%} of its receptor contacts"
        )
    return reasons


def summarize_pose_stability(
    trajectory: PoseStabilityTrajectory,
    drift_threshold_angstrom: float,
    contact_retention_threshold: float,
) -> PoseStabilitySummary:
    rmsd_series = trajectory.rmsd_series_angstrom
    contact_series = trajectory.contact_retention_series
    if not rmsd_series:
        raise ValueError(f"no frames were recorded for {trajectory.compound_id}")
    final_rmsd = rmsd_series[-1]
    maximum_rmsd = max(rmsd_series)
    final_contacts = contact_series[-1] if contact_series else 0.0
    sustained_contacts = sustained_tail_mean(contact_series, CONTACT_TAIL_FRACTION)
    left_pocket = pose_left_the_pocket(sustained_contacts, contact_retention_threshold)
    drifted = pose_drifted(final_rmsd, drift_threshold_angstrom)
    verdict = "unstable" if left_pocket else "drifted" if drifted else "stable"
    return PoseStabilitySummary(
        compound_id=trajectory.compound_id,
        verdict=verdict,
        reasons=stability_reasons(
            final_rmsd,
            maximum_rmsd,
            sustained_contacts,
            drift_threshold_angstrom,
            contact_retention_threshold,
        ),
        frames=len(rmsd_series),
        final_rmsd_angstrom=final_rmsd,
        mean_rmsd_angstrom=mean_of(rmsd_series),
        maximum_rmsd_angstrom=maximum_rmsd,
        final_contact_retention=final_contacts,
        mean_contact_retention=mean_of(contact_series),
        sustained_contact_retention=sustained_contacts,
        affinity_rank=trajectory.affinity_rank,
        best_affinity_kcal_per_mol=trajectory.best_affinity_kcal_per_mol,
        heavy_atoms=trajectory.heavy_atoms,
    )


def rank_by_pose_stability(summaries: list[PoseStabilitySummary]) -> list[PoseStabilitySummary]:
    order = {"stable": 0, "drifted": 1, "unstable": 2}
    return sorted(
        summaries,
        key=lambda summary: (
            order[summary.verdict],
            summary.final_rmsd_angstrom,
            summary.compound_id,
        ),
    )


def stability_verdict_counts(summaries: list[PoseStabilitySummary]) -> dict[str, int]:
    counts = {"stable": 0, "drifted": 0, "unstable": 0}
    for summary in summaries:
        counts[summary.verdict] += 1
    return counts


def pose_stability_separates_compounds(summaries: list[PoseStabilitySummary]) -> bool:
    return len({summary.verdict for summary in summaries}) > 1
