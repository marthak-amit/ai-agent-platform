"""
Simulator router — the in-dashboard chat simulator (Owner only).

Endpoints:
- GET  /simulator/session : transcript + settings, to resume after a page reload
- POST /simulator/message : play one customer turn through the REAL router + order engine (nothing is sent anywhere)
- POST /simulator/reset   : wipe the simulator chat and everything it created

Unlimited and uncounted by design (never billed, never part of the trial). See app/services/simulator_service.py.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.client import Client
from app.routers.auth import get_owner_client
from app.schemas.simulator import (
    SimulatorMessageRequest,
    SimulatorReplyOut,
    SimulatorResetOut,
    SimulatorSessionOut,
)
from app.services import simulator_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/simulator", tags=["simulator"])


@router.get("/session", response_model=SimulatorSessionOut)
async def get_session(
    client: Annotated[Client, Depends(get_owner_client)],
    db: AsyncSession = Depends(get_db),
) -> SimulatorSessionOut:
    """Return the simulator transcript, whether the agent test is passed, and the router/limit settings."""
    return await simulator_service.session(db, client)


@router.post("/message", response_model=SimulatorReplyOut)
async def post_message(
    body: SimulatorMessageRequest,
    client: Annotated[Client, Depends(get_owner_client)],
    db: AsyncSession = Depends(get_db),
) -> SimulatorReplyOut:
    """
    Play one customer turn (typed text, a tapped button, or a picked list row) and return the bot's reply bubbles.

    Raises:
        HTTPException 429: SIMULATOR_DAILY_MESSAGE_LIMIT (if set) is used up.
    """
    try:
        return await simulator_service.run_turn(
            db, client, text=body.text, button_id=body.button_id, list_row_id=body.list_row_id, title=body.title,
        )
    except simulator_service.SimulatorLimitError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc


@router.post("/reset", response_model=SimulatorResetOut)
async def reset(
    client: Annotated[Client, Depends(get_owner_client)],
    db: AsyncSession = Depends(get_db),
) -> SimulatorResetOut:
    """Delete the simulator conversation and its simulated orders so the owner can start from a clean slate."""
    removed = await simulator_service.reset(db, client)
    return SimulatorResetOut(orders_removed=removed)
