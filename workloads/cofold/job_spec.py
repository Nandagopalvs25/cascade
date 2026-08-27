from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Workload = Literal["dock", "admet", "md_stability", "cofold"]
TargetSource = Literal["rcsb", "card_attachment", "url"]

DEFAULT_PROTENIX_MODEL = "protenix_base_default_v1.0.0"


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


class FoldParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model_name: str = DEFAULT_PROTENIX_MODEL
    seeds: list[int] = Field(default_factory=lambda: [101])
    cycles: int = Field(default=10, ge=1, le=20)
    diffusion_steps: int = Field(default=200, ge=1, le=1000)
    samples_per_seed: int = Field(default=5, ge=1, le=25)
    dtype: Literal["bf16", "fp32"] = "bf16"
    use_msa: bool = False
    max_complexes: int = Field(default=8, ge=1, le=64)
    prediction_timeout_seconds: int = Field(default=3000, ge=60)
    protein_sequence: str | None = None

    @classmethod
    def from_job_spec(cls, spec: JobSpec) -> "FoldParams":
        return cls.model_validate(spec.params)

    @property
    def seeds_argument(self) -> str:
        return ",".join(str(seed) for seed in self.seeds)
