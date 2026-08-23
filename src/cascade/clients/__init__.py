from cascade.clients.gcs import GCSClient
from cascade.clients.jobs import CloudRunJobClient
from cascade.clients.pubsub import PubSubPublisher
from cascade.clients.structures import CampaignInputResolver
from cascade.clients.trello import TrelloClient
from cascade.config import get_settings

settings = get_settings()

trello = TrelloClient(
    settings.trello_api_key, settings.trello_api_token, settings.trello_api_secret
)
pubsub = PubSubPublisher(settings.gcp_project_id)
gcs = GCSClient(settings.gcs_bucket)
cloud_run_jobs = CloudRunJobClient()
campaign_inputs = CampaignInputResolver(gcs, trello)
