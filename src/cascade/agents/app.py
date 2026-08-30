from google.adk.apps.app import App, ResumabilityConfig
from google.adk.plugins import ReflectAndRetryModelPlugin

from cascade.agents.campaign import cascade_campaign
from cascade.config import get_settings

settings = get_settings()

cascade_app = App(
    name="cascade",
    root_agent=cascade_campaign,
    resumability_config=ResumabilityConfig(is_resumable=True),
    plugins=[ReflectAndRetryModelPlugin(max_retries=settings.max_model_reflect_retries)],
)
