"""Activity 10: app_persist_activation."""
from typing import Any

from temporalio import activity

from ..models import PersistActivationInput
from . import repo
from .base import ActivityBase


class PersistActivation(ActivityBase):
    @activity.defn(name="app_persist_activation")
    async def app_persist_activation(self, inp: PersistActivationInput) -> dict[str, Any]:
        return await self._tx(repo.persist_activation, inp)
