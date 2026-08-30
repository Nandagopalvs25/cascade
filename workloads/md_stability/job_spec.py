from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    target: TargetStructure | None = None
    ligands_uri: str
    binding_site: BindingSite | None = None
    params: dict = Field(default_factory=dict)
    output_uri: str
    control_compound: str | None = None


class StabilityParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_complexes: int = Field(default=8, ge=1, le=64)
    equilibration_steps: int = Field(default=5000, ge=0, le=500_000)
    production_steps: int = Field(default=50_000, ge=1000, le=5_000_000)
    timestep_femtoseconds: float = Field(default=2.0, gt=0.0, le=4.0)
    temperature_kelvin: float = Field(default=300.0, gt=0.0, le=400.0)
    frames_recorded: int = Field(default=50, ge=5, le=1000)
    minimization_max_iterations: int = Field(default=1000, ge=0)
    pose_drift_threshold_angstrom: float = Field(default=2.5, gt=0.0, le=10.0)
    contact_retention_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    contact_cutoff_angstrom: float = Field(default=4.0, gt=0.0, le=10.0)
    contact_break_cutoff_angstrom: float = Field(default=5.5, gt=0.0, le=15.0)
    simulation_timeout_seconds: int = Field(default=1800, ge=60)

    @classmethod
    def from_job_spec(cls, spec: JobSpec) -> "StabilityParams":
        return cls.model_validate(spec.params)

    @model_validator(mode="after")
    def require_break_cutoff_beyond_contact_cutoff(self) -> "StabilityParams":
        if self.contact_break_cutoff_angstrom < self.contact_cutoff_angstrom:
            raise ValueError(
                "contact_break_cutoff_angstrom must be at least contact_cutoff_angstrom - a "
                "contact cannot break closer than the distance that formed it"
            )
        return self

    @property
    def production_picoseconds(self) -> float:
        return self.production_steps * self.timestep_femtoseconds / 1000.0


def require_target_structure(spec: JobSpec) -> TargetStructure:
    if spec.target is None:
        raise ValueError(
            f"the {spec.workload} container needs a protein structure, but the job spec carries "
            "none"
        )
    return spec.target
