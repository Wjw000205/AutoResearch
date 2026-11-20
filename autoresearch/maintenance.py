"""Supervise existing idempotent maintenance sweeps; never retry model requests."""
from __future__ import annotations

import asyncio
import logging


class Maintenance:
    def __init__(self, name, operation):
        self.name, self.operation = name, operation
        self.failed = False

    async def sweep(self):
        try:
            await self.operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.failed:
                logging.getLogger(__name__).warning('Maintenance %s deferred after %s', self.name, type(exc).__name__)
            self.failed = True
        else:
            if self.failed:
                logging.getLogger(__name__).info('Maintenance %s recovered', self.name)
            self.failed = False

    async def maintain(self, interval, *, initial_delay=False):
        if initial_delay:
            await asyncio.sleep(interval)
        while True:
            await self.sweep()
            await asyncio.sleep(interval)
