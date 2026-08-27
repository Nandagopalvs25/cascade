import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Workload = Literal["dock", "admet", "md_stability", "cofold"]
TargetSource = Literal["rcsb", "card_attachment", "url"]

RCSB_ID_PATTERN = re.compile(r"^[0-9][A-Za-z0-9]{3}$")


class BindingSite(BaseModel):
    center_x: float
    center_y: float
    center_z: float
    size_x: float = 20.0
    size_y: float = 20.0
    size_z: float = 20.0


class TargetStructure(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    source: TargetSource
    reference: str
    structure_uri: str
    pdb_id: str | None = None
    chain: str | None = None

    @field_validator("pdb_id")
    @classmethod
    def normalize_rcsb_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not RCSB_ID_PATTERN.match(value):
            raise ValueError(f"not a 4-character RCSB ID: {value!r}")
        return value.upper()

    @field_validator("structure_uri")
    @classmethod
    def require_resolved_gcs_uri(cls, value: str) -> str:
        if not value.startswith("gs://"):
            raise ValueError(f"structure_uri must be a resolved gs:// URI, got {value!r}")
        return value

    @model_validator(mode="after")
    def require_reference_consistent_with_source(self) -> "TargetStructure":
        if self.source == "rcsb":
            if self.pdb_id is None:
                raise ValueError("pdb_id is required when source is 'rcsb'")
        elif self.pdb_id is not None:
            raise ValueError(
                f"pdb_id is only meaningful when source is 'rcsb', not {self.source!r}"
            )

        if self.source == "url" and not self.reference.startswith(("http://", "https://")):
            raise ValueError(
                f"reference must be an http(s) URL when source is 'url': {self.reference!r}"
            )

        if not self.reference:
            raise ValueError("reference must not be empty")

        return self


class JobSpec(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    run_id: str
    workload: Workload
    target: TargetStructure
    ligands_uri: str
    binding_site: BindingSite | None = None
    params: dict = Field(default_factory=dict)
    output_uri: str
    control_compound: str | None = None


class TargetRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    run_id: str
    card_id: str
    source: TargetSource
    reference: str
    chain: str | None = None
