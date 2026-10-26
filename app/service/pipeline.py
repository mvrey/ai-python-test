"""Asynchronous processing pipeline: extraction queue -> AI + guardrails -> notify queue -> provider.

Only the intake side exists so far; the workers are added in a later step.
"""

import asyncio

from service.config import Settings


class Pipeline:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._extraction_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=settings.queue_maxsize)

    def submit(self, request_id: str) -> bool:
        """Enqueue a request for extraction without blocking. Returns False if the queue is full."""
        try:
            self._extraction_queue.put_nowait(request_id)
        except asyncio.QueueFull:
            return False
        return True

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass
