from google.adk.apps.app import App, ResumabilityConfig

from cascade.agents.campaign import cascade_campaign

cascade_app = App(
    name="cascade",
    root_agent=cascade_campaign,
    resumability_config=ResumabilityConfig(is_resumable=True),
)
