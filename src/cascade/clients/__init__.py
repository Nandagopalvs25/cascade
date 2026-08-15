from cascade.clients.trello import TrelloClient
from cascade.config import get_settings

settings = get_settings()

trello = TrelloClient(
    settings.trello_api_key, settings.trello_api_token, settings.trello_api_secret
)
