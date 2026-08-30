from google.adk.agents import Agent
from google.adk.models.google_llm import Gemini
from google.genai import types

from cascade.agents.decision_tools import (
    read_previous_stage_compound_measurements,
    read_previous_stage_conclusion,
    read_previous_stage_request_card,
)
from cascade.agents.prompts import (
    INTAKE_INSTRUCTION,
    PLANNER_INSTRUCTION,
    STAGE_DECISION_INSTRUCTION,
    TRIAGE_INSTRUCTION,
)
from cascade.agents.schemas import (
    CampaignIntent,
    CardInputs,
    PlanRequest,
    StageDecision,
    StageDecisionRequest,
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


stage_decision_agent = Agent(
    name="stage_decision",
    model=build_gemini_model(settings.gemini_model),
    mode="single_turn",
    description=(
        "Decides which computational stage CASCADE runs next, or that it runs none, and writes "
        "the card that proposes it."
    ),
    instruction=STAGE_DECISION_INSTRUCTION,
    generate_content_config=REASONING_KEPT_OUT_OF_OUTPUT_FIELDS,
    input_schema=StageDecisionRequest,
    output_schema=StageDecision,
    tools=[
        read_previous_stage_conclusion,
        read_previous_stage_compound_measurements,
        read_previous_stage_request_card,
    ],
)
