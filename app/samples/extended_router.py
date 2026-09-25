from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection, transaction
from app.samples.extended_schemas import (
    CollectionCreate,
    DestructionExecute,
    DispositionCreate,
    InventoryCount,
    InventoryStart,
    RereviewConfirm,
    TransferCreate,
)
from app.samples.inventory import InventoryService, StockSummaryService
from app.samples.operations import CollectionService, DestructionService, LineageService, TransferService
from app.samples.reporting import BatchReconciliationService, ExceptionAgingService

router = APIRouter(prefix="/api/sample-operations", tags=["样品作业"])


@router.post("/collections", status_code=status.HTTP_201_CREATED)
def register_collection(payload: CollectionCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return CollectionService(connection).register(principal, payload.model_dump())


@router.post("/{sample_id}/transfers")
def transfer_sample(sample_id: int, payload: TransferCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return TransferService(connection).move(principal, sample_id, payload.model_dump())


@router.get("/{sample_id}/lineage")
def sample_lineage(sample_id: int, principal: Principal = Depends(current_principal)):
    return LineageService(get_connection()).graph(principal, sample_id)


@router.post("/inventory", status_code=status.HTTP_201_CREATED)
def start_inventory(payload: InventoryStart, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return InventoryService(connection).start(principal, payload.location_id, payload.session_code)


@router.post("/inventory/{session_id}/counts")
def record_count(session_id: int, payload: InventoryCount, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return InventoryService(connection).count(
            principal,
            session_id,
            payload.sample_id,
            payload.observed_present,
            payload.observed_quantity,
            payload.note,
        )


@router.post("/inventory/{session_id}/reconcile")
def reconcile_inventory(session_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return InventoryService(connection).reconcile(principal, session_id)


@router.post("/inventory/{session_id}/close")
def close_inventory(session_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return InventoryService(connection).close(principal, session_id)


@router.post("/inventory/{session_id}/dispositions", status_code=status.HTTP_201_CREATED)
def create_disposition(session_id: int, payload: DispositionCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return InventoryService(connection).create_disposition(principal, session_id, payload.model_dump())


@router.get("/inventory/{session_id}/dispositions")
def list_dispositions(session_id: int, principal: Principal = Depends(current_principal)):
    return InventoryService(get_connection()).list_dispositions(principal, session_id)


@router.post("/inventory/dispositions/{disposition_id}/recheck")
def recheck_disposition(disposition_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return InventoryService(connection).recheck_disposition(principal, disposition_id)


@router.post("/inventory/dispositions/{disposition_id}/rereview")
def confirm_rereview(
    disposition_id: int,
    payload: RereviewConfirm,
    principal: Principal = Depends(current_principal),
):
    with transaction(immediate=True) as connection:
        return InventoryService(connection).confirm_rereview(
            principal, disposition_id, payload.note
        )


@router.post("/inventory/dispositions/{disposition_id}/execute")
def execute_disposition(disposition_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return InventoryService(connection).execute_adjustment(principal, disposition_id)


@router.get("/stock/by-location")
def stock_by_location(principal: Principal = Depends(current_principal)):
    return StockSummaryService(get_connection()).by_location(principal)


@router.get("/stock/by-state")
def stock_by_state(principal: Principal = Depends(current_principal)):
    return StockSummaryService(get_connection()).by_state(principal)


@router.post("/destructions/{request_id}", status_code=status.HTTP_201_CREATED)
def execute_destruction(
    request_id: int,
    payload: DestructionExecute,
    principal: Principal = Depends(current_principal),
):
    with transaction(immediate=True) as connection:
        return DestructionService(connection).execute(principal, request_id, payload.model_dump())


@router.get("/batches/{batch_id}/reconciliation")
def batch_reconciliation(batch_id: int, principal: Principal = Depends(current_principal)):
    return BatchReconciliationService(get_connection()).detail(principal, batch_id)


@router.get("/batches/open")
def open_batches(principal: Principal = Depends(current_principal)):
    return BatchReconciliationService(get_connection()).open_batches(principal)


@router.get("/exceptions/aging")
def exception_aging(principal: Principal = Depends(current_principal)):
    return ExceptionAgingService(get_connection()).summary(principal)
