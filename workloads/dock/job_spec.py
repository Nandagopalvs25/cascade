from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Workload = Literal["dock", "admet", "md_stability", "fold_affinity"]
TargetSource = Literal["rcsb", "card_attachment", "url"]


class BindingSite(BaseModel):
    model_config = ConfigDict(extra="ignore")

    center_x: float
    center_y: float
    center_z: float
    size_x: float = 20.0
    size_y: float = 20.0
    size_z: float = 20.0

    @property
    def center(self) -> list[float]:
        return [self.center_x, self.center_y, self.center_z]

    @property
    def box_size(self) -> list[float]:
        return [self.size_x, self.size_y, self.size_z]


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


class DockingParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    exhaustiveness: int = Field(default=8, ge=1, le=512)
    num_modes: int = Field(default=9, ge=1, le=50)
    seed: int = 42
    cpu: int = Field(default=0, ge=0)
    receptor_ph: float = Field(default=7.4, ge=0.0, le=14.0)
    binding_site_padding: float = Field(default=5.0, ge=0.0, le=20.0)
    max_ligands: int = Field(default=500, ge=1)

    @classmethod
    def from_job_spec(cls, spec: JobSpec) -> "DockingParams":
        return cls.model_validate(spec.params)
