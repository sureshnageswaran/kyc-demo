"""Register the typed custom Search Attributes (spec section 16).

Idempotent. Run directly (python -m temporal.cli.search_attributes) or let the
worker call it at startup.
"""
import asyncio
import logging

from temporalio.api.enums.v1 import IndexedValueType
from temporalio.api.operatorservice.v1 import AddSearchAttributesRequest, ListSearchAttributesRequest
from temporalio.client import Client

log = logging.getLogger("kyc.search_attributes")

ATTRIBUTES = {
    "KycApplicationId": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD,
    "KycBusinessStatus": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD,
    "KycStage": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD,
    "KycWaitingReason": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD,
    "KycPolicyVersionInt": IndexedValueType.INDEXED_VALUE_TYPE_INT,
    "KycReviewDeadlineDate": IndexedValueType.INDEXED_VALUE_TYPE_DATETIME,
}


async def register_search_attributes(client: Client) -> None:
    ns = client.namespace
    try:
        existing = await client.operator_service.list_search_attributes(ListSearchAttributesRequest(namespace=ns))
    except Exception as exc:  # Temporal Cloud does not expose the operator API on the namespace endpoint
        log.warning("cannot list search attributes (%s); on Temporal Cloud create them with "
                    "`tcld namespace search-attributes add` or in the UI: %s",
                    type(exc).__name__, ", ".join(ATTRIBUTES))
        return
    have = set(existing.custom_attributes) | set(existing.system_attributes)
    missing = {k: v for k, v in ATTRIBUTES.items() if k not in have}
    if not missing:
        return
    try:
        await client.operator_service.add_search_attributes(
            AddSearchAttributesRequest(namespace=ns, search_attributes=missing))
        log.info("registered search attributes: %s", sorted(missing))
    except Exception as exc:  # e.g. insufficient permissions on Temporal Cloud
        log.warning("could not register search attributes %s (%s); register them manually",
                    sorted(missing), type(exc).__name__)


if __name__ == "__main__":
    from .config import Settings
    from .worker import connect

    async def _main() -> None:
        await register_search_attributes(await connect(Settings.from_env()))

    asyncio.run(_main())
