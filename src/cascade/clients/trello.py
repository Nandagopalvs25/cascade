import base64
import hashlib
import hmac

import httpx


class TrelloClient:
    BASE_URL = "https://api.trello.com/1"

    def __init__(self, api_key: str, api_token: str, api_secret: str):
        self._key = api_key
        self._token = api_token
        self._secret = api_secret
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            params={"key": api_key, "token": api_token},
            headers={"X-Trello-Client-Identifier": "cascade-agent"},
            timeout=10.0,
        )

    async def create_card(self, list_id: str, name: str, desc: str) -> dict:
        resp = await self._client.post(
            "/cards", json={"idList": list_id, "name": name, "desc": desc}
        )
        resp.raise_for_status()
        return resp.json()

    async def move_card(self, card_id: str, list_id: str) -> None:
        resp = await self._client.put(f"/cards/{card_id}", json={"idList": list_id})
        resp.raise_for_status()

    async def add_comment(self, card_id: str, text: str) -> dict:
        resp = await self._client.post(f"/cards/{card_id}/actions/comments", json={"text": text})
        resp.raise_for_status()
        return resp.json()

    async def get_attachments(self, card_id: str) -> list[dict]:
        resp = await self._client.get(f"/cards/{card_id}/attachments")
        resp.raise_for_status()
        return resp.json()

    async def download_attachment(self, url: str) -> bytes:
        headers = {
            "Authorization": f'OAuth oauth_consumer_key="{self._key}", oauth_token="{self._token}"'
        }
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.content

    @staticmethod
    def verify_signature(raw_body: bytes, callback_url: str, secret: str, signature: str) -> bool:
        content = raw_body + callback_url.encode()
        expected = base64.b64encode(
            hmac.new(secret.encode(), content, hashlib.sha1).digest()
        ).decode()
        return hmac.compare_digest(expected, signature or "")
