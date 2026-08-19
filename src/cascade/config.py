from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CASCADE_", env_file=".env", extra="ignore")

    database_url: str

    gcp_project_id: str
    pubsub_push_service_account: str
    pubsub_push_audience: str
    pubsub_card_events_topic: str = "card-events"
    pubsub_job_completions_topic: str = "job-completions"

    trello_api_key: str
    trello_api_token: str
    trello_api_secret: str
    trello_board_id: str
    trello_callback_url: str

    trello_list_todo: str
    trello_list_in_progress: str
    trello_list_recommended: str
    trello_list_needs_attention: str
    trello_list_done: str

    gemini_model: str = "gemini-3.7-flash"
    gemini_location: str = "global"
    adk_user_id: str = "cascade"
    max_llm_calls_per_invocation: int = 60


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
