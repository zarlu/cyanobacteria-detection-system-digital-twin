"""``POST /ingest``: contract v1 messages from nodes, the analyzer, connectors and the simulator."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import SessionDep, require_api_key
from api.ingest import IngestResult, ingest_readings
from contract.models import IngestEnvelope

router = APIRouter(tags=["ingest"])


@router.post("/ingest", response_model=IngestResult, dependencies=[Depends(require_api_key)])
def ingest(message: IngestEnvelope, session: SessionDep) -> IngestResult:
    """Validate each reading on its own, run QC, insert idempotently.

    Valid readings are stored even when others in the batch are rejected. Re-sending a message
    changes nothing and reports the readings as ``duplicates``.
    """
    return ingest_readings(session, message.node_id, message.source, message.readings)
