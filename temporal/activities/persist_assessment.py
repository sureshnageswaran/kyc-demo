"""Activity 7: app_persist_assessment."""
from temporalio import activity

from ..models import AssessmentPersisted, PersistAssessmentInput
from . import repo
from .base import ActivityBase


class PersistAssessment(ActivityBase):
    @activity.defn(name="app_persist_assessment")
    async def app_persist_assessment(self, inp: PersistAssessmentInput) -> AssessmentPersisted:
        return await self._tx(repo.persist_assessment, inp)
