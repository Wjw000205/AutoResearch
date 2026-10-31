"""Explicit agent handoffs; each node completes one review or one revision.

The caller journals exact inputs, independent decisions and versions. Replaying
the graph reuses that journal rather than resending completed paid requests.
"""
import asyncio
from typing import TypedDict

from langgraph.errors import NodeCancelledError
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context


class CommunicationState(TypedDict, total=False):
    phase: str
    payload: dict
    result: dict


async def _invoke(graph, payload, transition_limit):
    with tracing_context(enabled=False):
        try:
            return await graph.compile().ainvoke({'phase': 'review', 'payload': payload},
                                                  {'recursion_limit': transition_limit})
        except NodeCancelledError as exc:
            # Existing controllers and recovery callers use cancellation as a
            # pause, not a scientific rejection or a new paid assignment.
            if isinstance(exc.__cause__, asyncio.CancelledError):
                raise exc.__cause__ from None
            raise


async def communicate(review, revise, payload, *, transition_limit):
    graph = StateGraph(CommunicationState)
    graph.add_node('review', review)
    graph.add_node('revise', revise)
    graph.add_edge(START, 'review')
    graph.add_conditional_edges('review', lambda state: state['phase'],
                                {'review': 'review', 'revise': 'revise', 'done': END})
    graph.add_edge('revise', 'review')
    state = await _invoke(graph, payload, transition_limit)
    return state['result']


async def dispatch(handoff, payload, *, transition_limit):
    """Advance one persisted version/attempt per node, with explicit outcomes."""
    if transition_limit is None:
        # Unlimited delivery attempts have no graph recursion ceiling. Each
        # handoff still enforces the caller's execution/review/recovery gates.
        state = {'phase': 'review', 'payload': payload}
        with tracing_context(enabled=False):
            while True:
                await asyncio.sleep(0)  # Keep pause/cancellation responsive.
                state.update(await handoff(state))
                if state['phase'] == 'done':
                    return state['result']
                if state['phase'] != 'review':
                    raise ValueError(f"Unexpected handoff phase: {state['phase']}")
    graph = StateGraph(CommunicationState)
    graph.add_node('handoff', handoff)
    graph.add_edge(START, 'handoff')
    graph.add_conditional_edges('handoff', lambda state: state['phase'],
                                {'review': 'handoff', 'done': END})
    state = await _invoke(graph, payload, transition_limit)
    return state['result']
