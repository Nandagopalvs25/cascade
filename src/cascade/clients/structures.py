import httpx

from cascade.clients.gcs import GCSClient, run_inputs_prefix
from cascade.clients.trello import TrelloClient
from cascade.schemas import TargetRequest, TargetStructure

RCSB_DOWNLOAD_URL = "https://files.rcsb.org/download/{pdb_id}.pdb"
ALLOWED_URL_SCHEMES = ("http://", "https://")
ARCHIVED_STRUCTURE_FILENAME = "structure.pdb"
PDB_CONTENT_TYPE = "chemical/x-pdb"


class CampaignInputResolver:
    def __init__(
        self,
        gcs: GCSClient,
        trello: TrelloClient,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._gcs = gcs
        self._trello = trello
        self._timeout = timeout
        self._transport = transport

    async def resolve_target_structure(self, request: TargetRequest) -> TargetStructure:
        if request.source == "rcsb":
            content = await self.download_rcsb_entry(request.reference)
        elif request.source == "card_attachment":
            content = await self.download_named_card_attachment(request.card_id, request.reference)
        else:
            content = await self.download_from_url(request.reference)

        if not content.strip():
            raise ValueError(
                f"structure for {request.source} reference {request.reference!r} was empty"
            )

        structure_uri = await self._gcs.upload_bytes(
            f"{run_inputs_prefix(request.run_id)}/{ARCHIVED_STRUCTURE_FILENAME}",
            content,
            PDB_CONTENT_TYPE,
        )

        return TargetStructure(
            source=request.source,
            reference=request.reference,
            structure_uri=structure_uri,
            pdb_id=request.reference if request.source == "rcsb" else None,
            chain=request.chain,
        )

    async def download_rcsb_entry(self, pdb_id: str) -> bytes:
        return await self._download_bytes(RCSB_DOWNLOAD_URL.format(pdb_id=pdb_id.upper()))

    async def download_from_url(self, url: str) -> bytes:
        if not url.startswith(ALLOWED_URL_SCHEMES):
            raise ValueError(f"structure URL must be http(s), got {url!r}")
        return await self._download_bytes(url)

    async def download_named_card_attachment(self, card_id: str, attachment_name: str) -> bytes:
        attachments = await self._trello.get_attachments(card_id)
        for attachment in attachments:
            if attachment.get("name") == attachment_name:
                return await self._trello.download_attachment(attachment["url"])
        raise ValueError(f"card {card_id} has no attachment named {attachment_name!r}")

    async def _download_bytes(self, url: str) -> bytes:
        async with httpx.AsyncClient(
            timeout=self._timeout, follow_redirects=True, transport=self._transport
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.content
