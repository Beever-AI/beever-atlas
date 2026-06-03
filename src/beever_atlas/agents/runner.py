"""ADK Runner integration for FastAPI.

Provides Runner creation with InMemorySessionService and
session-per-request pattern for use in API route handlers.
"""

import uuid
from typing import Any

from google.adk.agents import BaseAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService, Session
from google.adk.workflow import BaseNode

# Module-level session service shared across the app
_session_service = InMemorySessionService()

APP_NAME = "beever_atlas"


def create_runner(agent: Any) -> Runner:
    """Create an ADK Runner for the given root agent or workflow node.

    Args:
        agent: The root ADK Agent (e.g., query_router_agent) for the
            agent-tree path, or a ``Workflow``/``BaseNode`` for the
            graph-based ingestion pipeline.

    Returns:
        A Runner instance configured with InMemorySessionService.

    Note:
        In ADK 2.x ``BaseAgent`` itself subclasses ``BaseNode``, so the
        agent check must come first — otherwise every ``LlmAgent`` would be
        routed through ``Runner(node=...)`` and silently change the QA
        execution path. Only non-agent graph objects (``Workflow``,
        ``JoinNode``) take the ``node=`` path.

        ``Runner(node=...)`` defaults ``app_name`` to ``node.name``, which
        would diverge from the ``APP_NAME`` used by ``create_session`` and
        cause SessionNotFoundError. We pass ``app_name=APP_NAME`` explicitly
        so both paths share one app namespace.
    """
    if isinstance(agent, BaseAgent):
        return Runner(
            agent=agent,
            app_name=APP_NAME,
            session_service=_session_service,
        )
    if isinstance(agent, BaseNode):
        return Runner(
            node=agent,
            app_name=APP_NAME,
            session_service=_session_service,
        )
    return Runner(
        agent=agent,
        app_name=APP_NAME,
        session_service=_session_service,
    )


async def create_session(
    user_id: str = "anonymous",
    state: dict | None = None,
    session_id: str | None = None,
) -> Session:
    """Create or retrieve a session.

    Args:
        user_id: User identifier from auth middleware.
        state: Optional initial session state (used by ingestion pipeline).
        session_id: If provided, attempt to retrieve an existing session
            first; only create a new one if it does not exist. ADK's
            InMemorySessionService raises AlreadyExistsError on duplicate
            session_id, so get-or-create is the only correct pattern here.

    Returns:
        An ADK Session.
    """
    if session_id is not None:
        existing = await _session_service.get_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
        if existing is not None:
            return existing

    actual_id = session_id if session_id is not None else str(uuid.uuid4())
    session = await _session_service.create_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=actual_id,
        state=state or {},
    )
    return session


def get_session_service() -> InMemorySessionService:
    """Return the shared session service instance."""
    return _session_service


async def run_agent(
    agent: BaseAgent,
    state: dict | None = None,
    message: str = "process",
) -> dict:
    """Run an agent standalone and return the final session state.

    Creates a fresh session, drives the agent to completion, and returns
    the resulting session state dict. Useful for invoking LlmAgents outside
    the main pipeline (e.g., coreference resolution, media description).

    Args:
        agent: The ADK agent to run.
        state: Initial session state (prompt context, input data).
        message: User message to send to the agent (required for LlmAgents).

    Returns:
        The final session state after agent completion.
    """
    from google.genai import types

    runner = create_runner(agent)
    session = await create_session(state=state)

    async for _event in runner.run_async(
        user_id=session.user_id,
        session_id=session.id,
        new_message=types.Content(
            role="user",
            parts=[types.Part(text=message)],
        ),
    ):
        pass  # Drive to completion

    # Re-fetch session to get final state with all deltas applied
    final_session = await _session_service.get_session(
        app_name=APP_NAME,
        user_id=session.user_id,
        session_id=session.id,
    )
    return dict(final_session.state) if final_session else {}
