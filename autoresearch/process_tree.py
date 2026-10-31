"""Identify process instances without trusting a reused Windows parent PID."""
from collections import deque
import math

import psutil


def owned_process(pid, started):
    """Return the original live process instance, never its later PID owner."""
    if type(pid) is not int or pid <= 0 or type(started) not in (int, float) or not math.isfinite(started) or started <= 0:
        return None
    try:
        process = psutil.Process(pid)
        if process.create_time() != started or not process.is_running():
            return None
        return process
    except psutil.Error:
        return None


def verified_children(parent):
    """Return live descendants whose birth times agree with every ancestry edge.

    Captured psutil identities protect later kill calls from further PID reuse.
    Parents precede their descendants, allowing cleanup in reverse order.
    """
    result, pending, seen = [], deque([parent]), set()
    while pending:
        current = pending.popleft()
        try:
            created = current.create_time()
            if not math.isfinite(created) or not current.is_running():
                continue
            children = current.children()
        except psutil.Error:
            continue
        seen.add((current.pid, created))
        for child in children:
            try:
                birth = child.create_time()
                identity = (child.pid, birth)
                if (not math.isfinite(birth) or birth < created or identity in seen
                        or child.ppid() != current.pid or not child.is_running()):
                    continue
                if not current.is_running():
                    break
            except psutil.Error:
                continue
            seen.add(identity)
            result.append(child)
            pending.append(child)
    return result
