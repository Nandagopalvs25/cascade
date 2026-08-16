import asyncio
import json

from google.cloud import pubsub_v1


class PubSubPublisher:
    def __init__(self, project_id: str):
        self._client = pubsub_v1.PublisherClient()
        self._project_id = project_id

    async def publish(self, topic_name: str, data: dict) -> str:
        topic_path = self._client.topic_path(self._project_id, topic_name)
        future = self._client.publish(topic_path, json.dumps(data).encode("utf-8"))
        return await asyncio.wrap_future(future)
