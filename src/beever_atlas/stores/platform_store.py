"""MongoDB CRUD for PlatformConnection records."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorCollection

from beever_atlas.infra.crypto import decrypt_credentials, encrypt_credentials
from beever_atlas.models.platform_connection import PlatformConnection


class PlatformStore:
    """CRUD operations for platform connections stored in MongoDB.

    All credential fields are encrypted at rest via the crypto module.
    Credentials are never returned to callers — use decrypt_connection_credentials()
    when the raw plaintext is needed (e.g. to register an adapter).
    """

    def __init__(self, collection: AsyncIOMotorCollection) -> None:
        self._col = collection

    async def startup(self) -> None:
        """Create indexes for platform_connections collection."""
        # Drop old unique platform index if it exists — we now allow multiple
        # connections per platform.
        try:
            existing = await self._col.index_information()
            if "platform_1" in existing and existing["platform_1"].get("unique"):
                await self._col.drop_index("platform_1")
        except Exception:
            pass  # collection may not exist yet
        await self._col.create_index([("platform", 1), ("source", 1)])
        await self._col.create_index("source")
        # RES-177 H1: backfill legacy rows so multi-tenant operators have a
        # single sentinel to target when assigning ownership.
        await self.backfill_legacy_owners()

    async def backfill_legacy_owners(self) -> int:
        """Set ``owner_principal_id`` on legacy rows to the shared sentinel.

        Idempotent: only rewrites rows where the field is missing OR ``None``.
        Returns the number of rows updated. Safe to invoke on every boot;
        subsequent invocations against already-backfilled data return 0.
        """
        try:
            result = await self._col.update_many(
                {
                    "$or": [
                        {"owner_principal_id": {"$exists": False}},
                        {"owner_principal_id": None},
                    ]
                },
                {"$set": {"owner_principal_id": "legacy:shared"}},
            )
        except Exception:
            # Collection may not exist yet on a fresh install; nothing to do.
            return 0
        return int(getattr(result, "modified_count", 0) or 0)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _to_doc(self, conn: PlatformConnection) -> dict[str, Any]:
        """Serialize a PlatformConnection to a MongoDB document."""
        doc = conn.model_dump()
        # Motor requires bytes to be stored; they serialize fine as-is.
        return doc

    def _from_doc(self, doc: dict[str, Any]) -> PlatformConnection:
        """Deserialize a MongoDB document to a PlatformConnection."""
        doc.pop("_id", None)
        return PlatformConnection(**doc)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create_connection(
        self,
        platform: str,
        display_name: str,
        credentials: dict,
        selected_channels: list[str] | None = None,
        status: str = "connected",
        source: str = "ui",
        connection_id: str | None = None,
        owner_principal_id: str | None = None,
    ) -> PlatformConnection:
        """Encrypt credentials and persist a new PlatformConnection.

        ``owner_principal_id`` is stamped with the caller's principal id for
        UI-provisioned connections; env-provisioned rows pass the shared
        sentinel ``"legacy:shared"`` so single-tenant compatibility applies.
        """
        ciphertext, iv, tag = encrypt_credentials(credentials)
        kwargs: dict = dict(
            platform=platform,  # type: ignore[arg-type]
            display_name=display_name,
            encrypted_credentials=ciphertext,
            credential_iv=iv,
            credential_tag=tag,
            selected_channels=selected_channels or [],
            status=status,  # type: ignore[arg-type]
            source=source,  # type: ignore[arg-type]
            owner_principal_id=owner_principal_id,
        )
        if connection_id:
            kwargs["id"] = connection_id
        conn = PlatformConnection(**kwargs)
        await self._col.insert_one(self._to_doc(conn))
        return conn

    async def get_connection(self, connection_id: str) -> PlatformConnection | None:
        """Return a PlatformConnection by ID, or None if not found."""
        doc = await self._col.find_one({"id": connection_id})
        if doc is None:
            return None
        return self._from_doc(doc)

    async def list_connections(self) -> list[PlatformConnection]:
        """Return all platform connections."""
        connections: list[PlatformConnection] = []
        async for doc in self._col.find({}):
            connections.append(self._from_doc(doc))
        return connections

    async def list_connections_by_source(self, source: str) -> list[PlatformConnection]:
        """Return connections filtered by source ('ui' or 'env')."""
        connections: list[PlatformConnection] = []
        async for doc in self._col.find({"source": source}):
            connections.append(self._from_doc(doc))
        return connections

    async def update_connection(
        self,
        connection_id: str,
        *,
        status: str | None = None,
        error_message: str | None = None,
        selected_channels: list[str] | None = None,
        credentials: dict | None = None,
    ) -> PlatformConnection | None:
        """Partially update a connection. Returns the updated doc or None."""
        updates: dict[str, Any] = {"updated_at": datetime.now(tz=UTC)}

        if status is not None:
            updates["status"] = status
        if error_message is not None:
            updates["error_message"] = error_message
        if selected_channels is not None:
            updates["selected_channels"] = selected_channels
        if credentials is not None:
            ciphertext, iv, tag = encrypt_credentials(credentials)
            updates["encrypted_credentials"] = ciphertext
            updates["credential_iv"] = iv
            updates["credential_tag"] = tag

        result = await self._col.find_one_and_update(
            {"id": connection_id},
            {"$set": updates},
            return_document=True,
        )
        if result is None:
            return None
        return self._from_doc(result)

    async def get_connection_by_platform(self, platform: str) -> PlatformConnection | None:
        """Return a PlatformConnection by platform name, or None."""
        doc = await self._col.find_one({"platform": platform})
        if doc is None:
            return None
        return self._from_doc(doc)

    async def get_connections_by_platform_and_source(
        self,
        platform: str,
        source: str,
    ) -> list[PlatformConnection]:
        """Return connections filtered by both platform and source."""
        connections: list[PlatformConnection] = []
        async for doc in self._col.find({"platform": platform, "source": source}):
            connections.append(self._from_doc(doc))
        return connections

    async def delete_connection(self, connection_id: str) -> bool:
        """Delete a connection by ID. Returns True if deleted, False if not found."""
        result = await self._col.delete_one({"id": connection_id})
        return result.deleted_count > 0

    # ------------------------------------------------------------------
    # Credential access (internal use only)
    # ------------------------------------------------------------------

    def decrypt_connection_credentials(self, conn: PlatformConnection) -> dict:
        """Decrypt and return the plaintext credentials for a connection.

        Only call this when the raw credentials are needed (e.g. adapter registration).
        Never include the result in API responses.
        """
        return decrypt_credentials(
            conn.encrypted_credentials,
            conn.credential_iv,
            conn.credential_tag,
        )
