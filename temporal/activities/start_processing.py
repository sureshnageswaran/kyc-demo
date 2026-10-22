"""Activity 1: app_start_processing."""
from typing import Any

from temporalio import activity

from ..models import StartProcessingInput
from . import repo
from .base import ActivityBase


class StartProcessing(ActivityBase):
    @activity.defn(name="app_start_processing")
    async def app_start_processing(self, inp: StartProcessingInput) -> dict[str, Any]:
        return await self._tx(repo.start_processing, inp)
