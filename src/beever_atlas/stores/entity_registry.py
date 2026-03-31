"""Entity registry backed by Neo4jStore for canonical name resolution."""

from __future__ import annotations

from beever_atlas.stores.neo4j_store import Neo4jStore


class EntityRegistry:
    """Resolves and registers entity aliases using the Neo4j knowledge graph
    as the backing store. Entities in Neo4j ARE the registry."""

    def __init__(self, neo4j: Neo4jStore) -> None:
        self._neo4j = neo4j

    async def resolve_alias(
        self,
        name: str,
        entity_type: str,
        channel_id: str | None = None,
    ) -> str:
        """Return the canonical entity name for `name`, or `name` itself if
        no entity or alias match is found.

        Checks channel-scoped entities first (when channel_id is provided),
        then falls back to global scope.
        """
        canonical = await self.get_canonical(name)
        if canonical is not None:
            return canonical
        return name

    async def register_alias(
        self,
        alias: str,
        canonical: str,
        entity_type: str,
    ) -> None:
        """Append `alias` to the aliases array of the entity with name `canonical`.

        No-op if the entity does not exist.
        """
        await self._neo4j.execute_query(
            """
            MATCH (e:Entity {name: $canonical, type: $entity_type})
            SET e.aliases = CASE
                WHEN $alias IN coalesce(e.aliases, []) THEN e.aliases
                ELSE coalesce(e.aliases, []) + [$alias]
            END
            """,
            canonical=canonical,
            entity_type=entity_type,
            alias=alias,
        )

    async def get_canonical(self, name: str) -> str | None:
        """Find an entity by exact name or by alias. Returns the canonical
        (node) name, or None if no match is found."""
        records = await self._neo4j.execute_query(
            """
            MATCH (e:Entity)
            WHERE e.name = $name OR $name IN coalesce(e.aliases, [])
            RETURN e.name AS canonical
            LIMIT 1
            """,
            name=name,
        )
        if not records:
            return None
        return records[0]["canonical"]

    async def get_all_canonical(self) -> list[dict]:
        """Return all entities as dicts with name, type, and aliases.

        Intended for pipeline state injection.
        """
        records = await self._neo4j.execute_query(
            """
            MATCH (e:Entity)
            RETURN e.name AS name, e.type AS type,
                   coalesce(e.aliases, []) AS aliases
            ORDER BY e.name
            """
        )
        return [
            {
                "name": r["name"],
                "type": r["type"],
                "aliases": list(r["aliases"]),
            }
            for r in records
        ]

    async def fuzzy_match(
        self, name: str, threshold: float = 0.8
    ) -> list[tuple[str, float]]:
        """Return (canonical_name, score) pairs for entities similar to `name`.

        Delegates to Neo4jStore.fuzzy_match_entity via APOC Jaro-Winkler.
        """
        records = await self._neo4j.execute_query(
            """
            MATCH (e:Entity)
            WITH e, apoc.text.jaroWinklerDistance(e.name, $name) AS score
            WHERE score >= $threshold
            RETURN e.name AS name, score
            ORDER BY score DESC
            """,
            name=name,
            threshold=threshold,
        )
        return [(r["name"], float(r["score"])) for r in records]
