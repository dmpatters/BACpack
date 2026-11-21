from __future__ import annotations

from pathlib import Path
from typing import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from ..connectors import ConnectorPoller
from ..engine import EngineManager
from ..trending import TrendPruner


def get_session(request: Request) -> Iterator[Session]:
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        yield session


def get_session_factory(request: Request):
    return request.app.state.session_factory


def get_data_dir(request: Request) -> Path:
    return Path(request.app.state.data_dir)


def get_engine(request: Request) -> EngineManager:
    return request.app.state.engine


def get_trend_pruner(request: Request) -> TrendPruner:
    return request.app.state.trend_pruner


def get_connector_poller(request: Request) -> ConnectorPoller:
    return request.app.state.connector_poller
