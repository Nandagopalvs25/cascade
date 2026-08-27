from dataclasses import dataclass, field

from rdkit import Chem
from rdkit.Chem import FilterCatalog
from rdkit.Chem.FilterCatalog import FilterCatalogParams

from properties import CompoundProperties

BASIC_AMINE_SMARTS = (
    "[NX3;H0,H1,H2;!$(N[#6]=[O,S,N]);!$(N[#7,#8,#16]);!$(Nc);!$(N#*);!$([N+]);!$(N=*)]"
)

ALERT_CATALOG_NAMES = ("PAINS", "BRENK", "NIH")
MAXIMUM_REPORTED_ALERT_NAMES = 5


def _build_alert_catalogs() -> dict[str, FilterCatalog.FilterCatalog]:
    catalogs: dict[str, FilterCatalog.FilterCatalog] = {}
    for name in ALERT_CATALOG_NAMES:
        params = FilterCatalogParams()
        params.AddCatalog(getattr(FilterCatalogParams.FilterCatalogs, name))
        catalogs[name.lower()] = FilterCatalog.FilterCatalog(params)
    return catalogs


_ALERT_CATALOGS = _build_alert_catalogs()
_BASIC_AMINE_PATTERN = Chem.MolFromSmarts(BASIC_AMINE_SMARTS)


@dataclass
class StructuralAlertHits:
    catalog: str
    count: int
    descriptions: list[str] = field(default_factory=list)


@dataclass
class HergRiskAssessment:
    band: str
    basic_amine_count: int
    reason: str


def structural_alert_hits(mol: Chem.Mol) -> list[StructuralAlertHits]:
    hits: list[StructuralAlertHits] = []
    for catalog_name, catalog in _ALERT_CATALOGS.items():
        matches = catalog.GetMatches(mol)
        descriptions = sorted({match.GetDescription() for match in matches})
        hits.append(
            StructuralAlertHits(
                catalog=catalog_name,
                count=len(descriptions),
                descriptions=descriptions[:MAXIMUM_REPORTED_ALERT_NAMES],
            )
        )
    return hits


def alert_count_by_catalog(hits: list[StructuralAlertHits]) -> dict[str, int]:
    return {hit.catalog: hit.count for hit in hits}


def count_basic_amines(mol: Chem.Mol) -> int:
    return len(mol.GetSubstructMatches(_BASIC_AMINE_PATTERN))


def assess_herg_risk(
    mol: Chem.Mol,
    properties: CompoundProperties,
    logp_threshold: float,
    minimum_aromatic_rings: int,
) -> HergRiskAssessment:
    basic_amines = count_basic_amines(mol)
    lipophilic = properties.crippen_logp >= logp_threshold
    aromatic = properties.aromatic_rings >= minimum_aromatic_rings

    if basic_amines and lipophilic and aromatic:
        return HergRiskAssessment(
            band="high",
            basic_amine_count=basic_amines,
            reason=(
                f"protonatable amine plus cLogP {properties.crippen_logp} "
                f"and {properties.aromatic_rings} aromatic rings matches the hERG pharmacophore"
            ),
        )
    if basic_amines and (lipophilic or aromatic):
        return HergRiskAssessment(
            band="moderate",
            basic_amine_count=basic_amines,
            reason=(
                f"protonatable amine present but only one of cLogP >= {logp_threshold} "
                f"or >= {minimum_aromatic_rings} aromatic rings is met"
            ),
        )
    if basic_amines:
        return HergRiskAssessment(
            band="low",
            basic_amine_count=basic_amines,
            reason="protonatable amine present without the lipophilic aromatic context",
        )
    return HergRiskAssessment(
        band="low",
        basic_amine_count=0,
        reason="no protonatable amine, the anchor of the hERG pharmacophore",
    )
