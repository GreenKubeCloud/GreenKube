# src/greenkube/api/routers/nodes.py
"""
API routes for Kubernetes node information.
"""

import logging
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, Query

from greenkube.api.dependencies import get_node_repository
from greenkube.models.node import NodeInfo
from greenkube.storage.base_repository import NodeRepository

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/nodes", response_model=List[NodeInfo])
async def list_nodes(
    include_inactive: bool = Query(False, description="Include nodes whose latest state is inactive."),
    repo: NodeRepository = Depends(get_node_repository),
):
    """Return the latest snapshot of all known Kubernetes nodes."""
    now = datetime.now(timezone.utc)
    nodes = await repo.get_latest_snapshots_before(now, include_inactive=include_inactive)
    return nodes
