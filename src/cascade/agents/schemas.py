from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Stage = Literal["fold", "dock", "admet", "md_stability"]


class CardTrigger(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    campaign_id: str
    card_id: str
    action_id: str


class CardInputs(BaseModel):
    card_id: str
    title: str
    description: str
    attachment_names: list[str] = Field(default_factory=list)


class CampaignIntent(BaseModel):
    target_name: str | None = None
    requested_stages: list[Stage] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    rationale: str


class IntakeAnnouncement(BaseModel):
    card_id: str
    intent: CampaignIntent


class CampaignOutcome(BaseModel):
    status: Literal["acknowledged", "needs_clarification", "duplicate"]
    campaign_id: str
    card_id: str
    target_name: str | None = None
    rationale: str
