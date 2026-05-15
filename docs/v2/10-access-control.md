# Access Control

Access control in Beever Atlas is **membership-based**: users can only see data from channels they are a member of on the originating platform. This applies to both private AND public channels — a user who has not joined `#backend` cannot see its memories, even if the channel is public.

This is critical for cross-channel search: when `channel_id` is omitted from a query, the system searches across ALL channels the user is a member of, not all channels in the workspace.

---

## ChannelACL

```python
class ChannelACL:
    """Access control based on platform channel membership.

    Membership-based model: users can only access channels they are a member of.
    This applies to BOTH private and public channels. A user who hasn't joined
    a public channel cannot see its memories through Beever Atlas.

    This is stricter than Slack's native model (where public channels are readable
    by all workspace members) but matches the user expectation that "I should only
    see data from channels I'm in."
    """

    # MongoDB collection: channel_acl
    # {channel_id, platform, is_private, member_ids, last_synced}

    async def sync_from_platform(self, channel_id: str, platform: str):
        """Pull current membership from platform API."""
        if platform == "slack":
            members = await self.slack.conversations_members(channel=channel_id)
            info = await self.slack.conversations_info(channel=channel_id)
            is_private = info["channel"]["is_private"]
        # ... similar for Teams, Discord

        await self.collection.update_one(
            {"channel_id": channel_id},
            {"$set": {"is_private": is_private,
                      "member_ids": members,
                      "last_synced": datetime.utcnow()}},
            upsert=True)

    async def check_access(self, user_id: str, channel_id: str) -> bool:
        """Check if user is a member of the channel. Applies to ALL channels."""
        acl = await self.collection.find_one({"channel_id": channel_id})
        if not acl:
            return False  # Unknown channel → deny
        return user_id in acl.get("member_ids", [])

    async def get_accessible_channels(self, user_id: str) -> list[str]:
        """Get all channel_ids the user is a member of.
        Used for cross-channel search when channel_id is omitted."""
        docs = await self.collection.find(
            {"member_ids": user_id}
        ).to_list()
        return [d["channel_id"] for d in docs]

    async def filter_results(self, user_id: str, results: list) -> list:
        """Remove results from channels the user is not a member of."""
        accessible = set(await self.get_accessible_channels(user_id))
        return [r for r in results if r.get("channel_id") in accessible]
```

Implemented in `src/beever_atlas/infra/access_control.py`.

---

## Integration Points

- **API authentication**: Bearer token middleware validates user identity before any operation
- **Retrieval pipeline**: `semantic_agent` and `graph_agent` (ADK) call `acl.filter_results()` via their tool implementations before returning results
- **Wiki builder**: Private channel sections display `[restricted]` for unauthorized users instead of content
- **Neo4j traversal**: Global entities (Person, Technology, etc.) are visible to all, but relationships carrying `source_channel` from a private channel are filtered
- **ACL sync**: Membership is refreshed on each channel sync and cached for 1 hour

---

## Auth Middleware

```python
@app.middleware("http")
async def authenticate(request: Request, call_next):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        return JSONResponse(status_code=401, content={"error": "Missing auth token"})
    user = await verify_workspace_token(token)
    request.state.user_id = user.id
    request.state.workspace_id = user.workspace_id
    return await call_next(request)
```

All routes receive `request.state.user_id` and `request.state.workspace_id` after the middleware runs. ACL checks downstream use `user_id` to gate results from private channels.
