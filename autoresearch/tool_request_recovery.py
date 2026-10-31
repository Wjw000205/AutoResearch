"""Bind newly audited successor packets without altering original requests."""
from __future__ import annotations


def prepared_successor(store, handoff_id, packet):
    """Seal a new strict packet; legacy packets retain their exact structure.

    Call only while creating a successor after its recovery gates passed. This
    is never a backfill or authentication repair for an already-dispatched
    original request, and does not grant tools or change its prepared schema.
    """
    version = store.get('handoff_inputs', handoff_id, {}).get('tool_contract_version', 0)
    if type(version) is int and version == 1:
        from .engine import digest
        return {**packet, 'prepared_sha256': digest(packet['prepared'])}
    return packet
