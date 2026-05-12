# Entity Dedup Runbook

This runbook documents how to collapse pre-existing duplicate
`(name, type, scope)` Entity triples in Neo4j **before** the PR-2
composite UNIQUE constraint (`entity_name_type_scope_unique`) is
created. The constraint creation will fail if any pre-existing
duplicates remain.

> **When to run this:** before deploying any build that calls
> `Neo4jStore.ensure_entity_name_type_scope_unique_constraint` for the
> first time on a database that already contains data — i.e. before the
> PR-2 (`NEO4J_RELATIONSHIP_STUB_ENDPOINTS=true`) rollout. After PR-2
> ships, the constraint blocks new duplicates from being created.

`Neo4jStore.ensure_entity_name_type_scope_unique_constraint` runs the
discovery query and APOC dedup automatically at startup. The procedure
below is the manual fallback when APOC is unavailable, when a
maintainer wants to inspect duplicates before merging, or when running
against a managed Neo4j instance that disables driver-side procedure
calls.

---

## 1. Discovery

Run this query in `cypher-shell` (or the Neo4j Browser) to enumerate
every duplicate group. **Empty result → no migration needed, proceed
straight to the constraint.**

```cypher
MATCH (e:Entity)
WITH e.name AS n, e.type AS t, e.scope AS s, collect(e) AS dups
WHERE size(dups) > 1
RETURN n, t, s, [d IN dups | elementId(d)] AS ids, size(dups) AS cnt
ORDER BY cnt DESC;
```

Save the output. Each row is one duplicate group — `cnt` Entity nodes
that share `(name, type, scope)` and will violate the new constraint.

---

## 2. APOC dedup (preferred)

If the deployment has the APOC plugin installed (`CALL apoc.help('apoc')`
returns rows), this single query collapses every duplicate group into
one canonical node, re-linking every relationship to the survivor:

```cypher
MATCH (e:Entity)
WITH e.name AS n, e.type AS t, e.scope AS s, collect(e) AS dups
WHERE size(dups) > 1
CALL apoc.refactor.mergeNodes(dups, {properties: 'discard', mergeRels: true})
YIELD node
RETURN n, t, s, elementId(node) AS kept;
```

`properties: 'discard'` keeps the first node's properties (Neo4j picks
deterministically by internal id); `mergeRels: true` re-links every
inbound and outbound relationship onto the survivor. After this query
returns, re-run the **Discovery** query — the expected output is zero
rows.

---

## 3. Manual fallback (no APOC)

If APOC is unavailable, dedup each group manually. For each row from
the discovery query, run:

```cypher
// Replace these literals from the discovery output:
//   $name  - the duplicate name
//   $type  - the duplicate type
//   $scope - the duplicate scope
//   $kept_id - elementId of the node you want to KEEP (typically
//              the oldest by created_at)
MATCH (keep:Entity)
WHERE elementId(keep) = $kept_id

MATCH (dup:Entity {name: $name, type: $type, scope: $scope})
WHERE elementId(dup) <> $kept_id

// Re-link inbound edges to keep
OPTIONAL MATCH (in)-[r_in]->(dup)
WITH keep, dup, collect({src: in, type: type(r_in), props: properties(r_in), rel: r_in}) AS inbound
FOREACH (item IN inbound |
  CREATE (item.src)-[r:_TEMP]->(keep)
  SET r = item.props
  DELETE item.rel
)
// Convert temp edges back to their actual types via APOC if present,
// otherwise script per (name, type, scope) group with a follow-up
// per-rel-type pass — see Neo4j 5 manual §4.5 "Refactoring" for the
// `CALL apoc.refactor.setType` pattern.

// Re-link outbound edges to keep
WITH keep, dup
OPTIONAL MATCH (dup)-[r_out]->(target)
WITH keep, dup, collect({tgt: target, type: type(r_out), props: properties(r_out), rel: r_out}) AS outbound
FOREACH (item IN outbound |
  CREATE (keep)-[r:_TEMP]->(item.tgt)
  SET r = item.props
  DELETE item.rel
)

DETACH DELETE dup
RETURN $name AS name, $type AS type, $scope AS scope;
```

The `_TEMP` placeholder type exists because Cypher does not support
parameterised relationship types in `CREATE`. After the above, you
must restore the original relationship types — either by APOC
(`apoc.refactor.setType`) or by exporting + re-importing the affected
edges.

> **Note:** This fallback is intentionally tedious. If you reach this
> step routinely, install APOC.

---

## 4. Verify

Re-run the **Discovery** query (§1). Expected: zero rows.

Then attempt the constraint:

```cypher
CREATE CONSTRAINT entity_name_type_scope_unique IF NOT EXISTS
FOR (e:Entity)
REQUIRE (e.name, e.type, e.scope) IS UNIQUE;
```

Confirm with:

```cypher
SHOW CONSTRAINTS YIELD name, type, entityType, labelsOrTypes, properties
WHERE name = 'entity_name_type_scope_unique';
```

The expected row:

```
name                            | type       | entityType | labelsOrTypes | properties
entity_name_type_scope_unique   | UNIQUENESS | NODE       | ['Entity']    | ['name', 'type', 'scope']
```

State must read `ONLINE`. If it reads `FAILED`, re-run the discovery
query — a row was inserted between the dedup and the constraint
creation.

---

## 5. Rollback

If PR-2 needs to be reverted in production:

```cypher
DROP CONSTRAINT entity_name_type_scope_unique IF EXISTS;
```

Then set `NEO4J_RELATIONSHIP_STUB_ENDPOINTS=false` and restart.

The constraint drop is irreversible only in the sense that any future
re-creation requires re-running the dedup procedure first.
