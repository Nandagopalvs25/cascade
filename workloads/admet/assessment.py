from dataclasses import dataclass, field

from rdkit import Chem

from alerts import (
    HergRiskAssessment,
    StructuralAlertHits,
    alert_count_by_catalog,
    assess_herg_risk,
    structural_alert_hits,
)
from job_spec import AdmetParams
from properties import CompoundProperties, compute_compound_properties

LIPINSKI_MAX_MOLECULAR_WEIGHT = 500.0
LIPINSKI_MAX_LOGP = 5.0
LIPINSKI_MAX_DONORS = 5
LIPINSKI_MAX_ACCEPTORS = 10

VEBER_MAX_ROTATABLE_BONDS = 10
VEBER_MAX_POLAR_SURFACE_AREA = 140.0

EGAN_MAX_POLAR_SURFACE_AREA = 131.6
EGAN_MAX_LOGP = 5.88

GHOSE_MOLECULAR_WEIGHT_RANGE = (160.0, 480.0)
GHOSE_LOGP_RANGE = (-0.4, 5.6)
GHOSE_HEAVY_ATOM_RANGE = (20, 70)
GHOSE_MOLAR_REFRACTIVITY_RANGE = (40.0, 130.0)

BLOOD_BRAIN_BARRIER_MAX_POLAR_SURFACE_AREA = 90.0
BLOOD_BRAIN_BARRIER_MAX_MOLECULAR_WEIGHT = 450.0

LOW_DRUG_LIKENESS_QED = 0.3
HIGH_SYNTHETIC_ACCESSIBILITY = 6.0

VERDICT_PASS = "pass"
VERDICT_FLAG = "flag"
VERDICT_FAIL = "fail"


@dataclass
class RuleSetOutcome:
    name: str
    passed: bool
    violations: list[str] = field(default_factory=list)


@dataclass
class CompoundAssessment:
    name: str
    smiles: str
    properties: CompoundProperties
    alerts: list[StructuralAlertHits]
    herg: HergRiskAssessment
    rule_sets: list[RuleSetOutcome]
    verdict: str
    reasons: list[str] = field(default_factory=list)

    @property
    def lipinski_violation_count(self) -> int:
        return len(self.rule_set("lipinski").violations)

    def rule_set(self, name: str) -> RuleSetOutcome:
        return next(outcome for outcome in self.rule_sets if outcome.name == name)


def evaluate_lipinski_rule_of_five(properties: CompoundProperties) -> RuleSetOutcome:
    violations: list[str] = []
    if properties.molecular_weight > LIPINSKI_MAX_MOLECULAR_WEIGHT:
        violations.append(f"MW {properties.molecular_weight} > {LIPINSKI_MAX_MOLECULAR_WEIGHT}")
    if properties.crippen_logp > LIPINSKI_MAX_LOGP:
        violations.append(f"cLogP {properties.crippen_logp} > {LIPINSKI_MAX_LOGP}")
    if properties.hydrogen_bond_donors > LIPINSKI_MAX_DONORS:
        violations.append(f"HBD {properties.hydrogen_bond_donors} > {LIPINSKI_MAX_DONORS}")
    if properties.hydrogen_bond_acceptors > LIPINSKI_MAX_ACCEPTORS:
        violations.append(f"HBA {properties.hydrogen_bond_acceptors} > {LIPINSKI_MAX_ACCEPTORS}")
    return RuleSetOutcome(name="lipinski", passed=len(violations) <= 1, violations=violations)


def evaluate_veber_oral_bioavailability(properties: CompoundProperties) -> RuleSetOutcome:
    violations: list[str] = []
    if properties.rotatable_bonds > VEBER_MAX_ROTATABLE_BONDS:
        violations.append(
            f"rotatable bonds {properties.rotatable_bonds} > {VEBER_MAX_ROTATABLE_BONDS}"
        )
    if properties.topological_polar_surface_area > VEBER_MAX_POLAR_SURFACE_AREA:
        violations.append(
            f"TPSA {properties.topological_polar_surface_area} > {VEBER_MAX_POLAR_SURFACE_AREA}"
        )
    return RuleSetOutcome(name="veber", passed=not violations, violations=violations)


def evaluate_egan_absorption(properties: CompoundProperties) -> RuleSetOutcome:
    violations: list[str] = []
    if properties.topological_polar_surface_area > EGAN_MAX_POLAR_SURFACE_AREA:
        violations.append(
            f"TPSA {properties.topological_polar_surface_area} > {EGAN_MAX_POLAR_SURFACE_AREA}"
        )
    if properties.crippen_logp > EGAN_MAX_LOGP:
        violations.append(f"cLogP {properties.crippen_logp} > {EGAN_MAX_LOGP}")
    return RuleSetOutcome(name="egan", passed=not violations, violations=violations)


def _outside_range(value: float, bounds: tuple[float, float], label: str) -> str | None:
    low, high = bounds
    if value < low or value > high:
        return f"{label} {value} outside {low}-{high}"
    return None


def evaluate_ghose_drug_likeness(properties: CompoundProperties) -> RuleSetOutcome:
    checks = [
        _outside_range(properties.molecular_weight, GHOSE_MOLECULAR_WEIGHT_RANGE, "MW"),
        _outside_range(properties.crippen_logp, GHOSE_LOGP_RANGE, "cLogP"),
        _outside_range(properties.heavy_atoms, GHOSE_HEAVY_ATOM_RANGE, "heavy atoms"),
        _outside_range(
            properties.molar_refractivity, GHOSE_MOLAR_REFRACTIVITY_RANGE, "molar refractivity"
        ),
    ]
    violations = [check for check in checks if check is not None]
    return RuleSetOutcome(name="ghose", passed=not violations, violations=violations)


def predicted_blood_brain_barrier_penetration(properties: CompoundProperties) -> str:
    if (
        properties.topological_polar_surface_area <= BLOOD_BRAIN_BARRIER_MAX_POLAR_SURFACE_AREA
        and properties.molecular_weight <= BLOOD_BRAIN_BARRIER_MAX_MOLECULAR_WEIGHT
    ):
        return "likely"
    return "unlikely"


def _decide_verdict(
    properties: CompoundProperties,
    alert_counts: dict[str, int],
    herg: HergRiskAssessment,
    rule_sets: list[RuleSetOutcome],
    params: AdmetParams,
) -> tuple[str, list[str]]:
    lipinski = next(outcome for outcome in rule_sets if outcome.name == "lipinski")
    blocking: list[str] = []
    concerns: list[str] = []

    if alert_counts.get("pains", 0):
        blocking.append(f"{alert_counts['pains']} PAINS assay-interference alert(s)")
    if alert_counts.get("brenk", 0) >= params.brenk_alerts_that_fail:
        blocking.append(f"{alert_counts['brenk']} Brenk structural alert(s)")
    if herg.band == "high":
        blocking.append(f"high hERG risk: {herg.reason}")
    if len(lipinski.violations) >= params.lipinski_violations_that_fail:
        blocking.append(f"{len(lipinski.violations)} Lipinski violation(s)")

    if blocking:
        return VERDICT_FAIL, blocking

    if alert_counts.get("brenk", 0):
        concerns.append(f"{alert_counts['brenk']} Brenk structural alert(s)")
    if alert_counts.get("nih", 0):
        concerns.append(f"{alert_counts['nih']} NIH structural alert(s)")
    if herg.band == "moderate":
        concerns.append(f"moderate hERG risk: {herg.reason}")
    if lipinski.violations:
        concerns.append(f"{len(lipinski.violations)} Lipinski violation(s)")
    for outcome in rule_sets:
        if outcome.name != "lipinski" and not outcome.passed:
            concerns.append(f"{outcome.name} violation(s): {', '.join(outcome.violations)}")
    if properties.drug_likeness_qed < LOW_DRUG_LIKENESS_QED:
        concerns.append(f"low drug-likeness QED {properties.drug_likeness_qed}")
    if properties.synthetic_accessibility > HIGH_SYNTHETIC_ACCESSIBILITY:
        concerns.append(f"high synthetic accessibility score {properties.synthetic_accessibility}")

    if concerns:
        return VERDICT_FLAG, concerns
    return VERDICT_PASS, ["no liabilities detected by the configured filters"]


def assess_compound(name: str, mol: Chem.Mol, params: AdmetParams) -> CompoundAssessment:
    properties = compute_compound_properties(mol)
    alerts = structural_alert_hits(mol)
    alert_counts = alert_count_by_catalog(alerts)
    herg = assess_herg_risk(
        mol, properties, params.herg_logp_threshold, params.herg_minimum_aromatic_rings
    )
    rule_sets = [
        evaluate_lipinski_rule_of_five(properties),
        evaluate_veber_oral_bioavailability(properties),
        evaluate_egan_absorption(properties),
        evaluate_ghose_drug_likeness(properties),
    ]
    verdict, reasons = _decide_verdict(properties, alert_counts, herg, rule_sets, params)
    return CompoundAssessment(
        name=name,
        smiles=Chem.MolToSmiles(mol),
        properties=properties,
        alerts=alerts,
        herg=herg,
        rule_sets=rule_sets,
        verdict=verdict,
        reasons=reasons,
    )
