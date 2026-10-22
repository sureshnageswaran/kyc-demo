"""Supporting activity: app_record_event."""
from typing import Any

from temporalio import activity

from ..models import RecordEventInput
from . import repo
from .base import ActivityBase


class RecordEvent(ActivityBase):
    @activity.defn(name="app_record_event")
    async def app_record_event(self, inp: RecordEventInput) -> dict[str, Any]:
        return await self._tx(repo.record_event, inp)
