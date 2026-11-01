"""Versioned author recovery bindings, excluding only workflow display state."""
from __future__ import annotations

import copy

from .implementation_review_routing import digest

SCOPE_VERSION = 2
_WORKFLOW_METADATA = frozenset({'updated_at', 'reason', 'blocked_kind'})


def workflow_snapshot(state):
    """Detach all scientific/identity fields, including unknown future fields."""
    if not isinstance(state, dict):
        raise ValueError('Author recovery workflow must be an object')
    return copy.deepcopy({key: value for key, value in state.items() if key not in _WORKFLOW_METADATA})


def workflow_fingerprint(state):
    return digest(workflow_snapshot(state))
