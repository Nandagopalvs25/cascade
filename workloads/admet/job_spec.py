from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Workload = Literal["dock", "admet", "md_stability", "cofold"]
TargetSource = Literal["rcsb", "card_attachment", "url"]


class BindingSite(BaseModel):
    model_config = ConfigDict(extra="ignore")

    center_x: float
    center_y: float
    center_z: float
    size_x: float = 20.0
    size_y: float = 20.0
    size_z: float = 20.0


class TargetStructure(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source: TargetSource
    reference: str
    structure_uri: str
    pdb_id: str | None = None
    chain: str | None = None


class JobSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    run_id: str
    workload: Workload
    target: TargetStructure
    ligands_uri: str
    binding_site: BindingSite | None = None
    params: dict = Field(default_factory=dict)
    output_uri: str
    control_compound: str | None = None


class AdmetParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_compounds: int = Field(default=2000, ge=1)
    herg_logp_threshold: float = Field(default=3.7, ge=0.0)
    herg_minimum_aromatic_rings: int = Field(default=2, ge=0)
    brenk_alerts_that_fail: int = Field(default=3, ge=1)
    lipinski_violations_that_fail: int = Field(default=2, ge=1)

    @classmethod
    def from_job_spec(cls, spec: JobSpec) -> "AdmetParams":
        return cls.model_validate(spec.params)
