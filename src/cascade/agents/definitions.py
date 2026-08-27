from google.adk.agents import Agent
from google.adk.models.google_llm import Gemini
from google.genai import types

from cascade.agents.prompts import (
    INTAKE_INSTRUCTION,
    PLANNER_INSTRUCTION,
    PROPOSER_INSTRUCTION,
    TRIAGE_INSTRUCTION,
)
from cascade.agents.schemas import (
    CampaignIntent,
    CardInputs,
    PlanRequest,
    ProposalRequest,
    StageProposal,
    TriageRequest,
    TriageVerdict,
    WorkloadPlan,
)
from cascade.config import get_settings

settings = get_settings()


REASONING_KEPT_OUT_OF_OUTPUT_FIELDS = types.GenerateContentConfig(
    thinking_config=types.ThinkingConfig(
        include_thoughts=True,
        thinking_level=types.ThinkingLevel.MEDIUM,
    )
)


def build_gemini_model(model_name: str) -> Gemini:
    return Gemini(
        model=model_name,
        client_kwargs={
            "enterprise": True,
            "project": settings.gcp_project_id,
            "location": settings.gemini_location,
        },
    )


intake_agent = Agent(
    name="intake",
    model=build_gemini_model(settings.gemini_model),
    mode="single_turn",
    description="Reads a Trello card and states what campaign it is requesting.",
    instruction=INTAKE_INSTRUCTION,
    generate_content_config=REASONING_KEPT_OUT_OF_OUTPUT_FIELDS,
    input_schema=CardInputs,
    output_schema=CampaignIntent,
)


planner_agent = Agent(
    name="planner",
    model=build_gemini_model(settings.gemini_model),
    mode="single_turn",
    description="Decides how to run one workload stage: search effort, binding site, and controls.",
    instruction=PLANNER_INSTRUCTION,
    generate_content_config=REASONING_KEPT_OUT_OF_OUTPUT_FIELDS,
    input_schema=PlanRequest,
    output_schema=WorkloadPlan,
)


triage_agent = Agent(
    name="triage",
    model=build_gemini_model(settings.gemini_model),
    mode="single_turn",
    description=(
        "Judges whether a finished workload can be trusted and what its numbers actually support."
    ),
    instruction=TRIAGE_INSTRUCTION,
    generate_content_config=REASONING_KEPT_OUT_OF_OUTPUT_FIELDS,
    input_schema=TriageRequest,
    output_schema=TriageVerdict,
)


proposer_agent = Agent(
    name="proposer",
    model=build_gemini_model(settings.gemini_model),
    mode="single_turn",
    description=(
        "Decides what stage should follow a finished one and writes the card that proposes it."
    ),
    instruction=PROPOSER_INSTRUCTION,
    generate_content_config=REASONING_KEPT_OUT_OF_OUTPUT_FIELDS,
    input_schema=ProposalRequest,
    output_schema=StageProposal,
)
