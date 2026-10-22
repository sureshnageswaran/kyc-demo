"""Activity 8: app_persist_final_outcome."""
from temporalio import activity

from ..models import FinalOutcomeInput, FinalOutcomePersisted
from . import repo
from .base import ActivityBase


class PersistFinalOutcome(ActivityBase):
    @activity.defn(name="app_persist_final_outcome")
    async def app_persist_final_outcome(self, inp: FinalOutcomeInput) -> FinalOutcomePersisted:
        return await self._tx(repo.persist_final_outcome, inp)
