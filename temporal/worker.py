"""Run the KYC worker: python -m temporal.worker"""
import asyncio
import logging

import httpx
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from temporalio.client import Client
from temporalio.worker import Worker

from .activities import KycActivities
from .config import Settings
from .cli.search_attributes import register_search_attributes
from .activities.storage import Storage
from .workflow import KycApplicationWorkflow

log = logging.getLogger("kyc.worker")


async def connect(settings: Settings) -> Client:
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        api_key=settings.temporal_api_key,
        tls=settings.temporal_tls or bool(settings.temporal_api_key),
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    client = await connect(settings)
    await register_search_attributes(client)

    pool = AsyncConnectionPool(settings.database_url, min_size=1, max_size=settings.db_pool_max,
                               kwargs={"row_factory": dict_row}, open=False)
    await pool.open(wait=True, timeout=15)
    async with httpx.AsyncClient() as http:
        acts = KycActivities(settings, pool, Storage(settings), http)
        worker = Worker(
            client,
            task_queue=settings.temporal_task_queue,
            workflows=[KycApplicationWorkflow],
            activities=acts.all_activities(),
            # Worker-wide cap on total processing across applications (spec section 6).
            max_concurrent_activities=settings.max_concurrent_activities,
            max_concurrent_workflow_tasks=settings.max_concurrent_workflow_tasks,
        )
        log.info("worker started: queue=%s namespace=%s", settings.temporal_task_queue,
                 settings.temporal_namespace)
        try:
            await worker.run()
        finally:
            await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
