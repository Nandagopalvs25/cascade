from google.adk.agents import Agent
from google.adk.models.google_llm import Gemini

from cascade.agents.prompts import INTAKE_INSTRUCTION, PLANNER_INSTRUCTION
from cascade.agents.schemas import CampaignIntent, CardInputs, PlanRequest, WorkloadPlan
from cascade.config import get_settings

settings = get_settings()


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
    input_schema=CardInputs,
    output_schema=CampaignIntent,
)


planner_agent = Agent(
    name="planner",
    model=build_gemini_model(settings.gemini_model),
    mode="single_turn",
    description="Decides how to run one workload stage: search effort, binding site, and controls.",
    instruction=PLANNER_INSTRUCTION,
    input_schema=PlanRequest,
    output_schema=WorkloadPlan,
)
