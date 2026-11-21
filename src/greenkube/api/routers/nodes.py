# src/greenkube/api/routers/nodes.py
"""
API routes for Kubernetes node information.
"""

import base64
import binascii
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from greenkube.api.dependencies import get_combined_metrics_repository, get_node_repository
from greenkube.api.schemas import NodeResponse, PaginatedNodesResponse
from greenkube.storage.base_repository import CombinedMetricsRepository, NodeRepository

logger = logging.getLogger(__name__)

router = APIRouter()

#: How far back the latest idle row of a node is looked up for its GPU power source.
GPU_SOURCE_LOOKBACK = timedelta(hours=1)


async def _latest_gpu_sources(repo: Any, now: datetime) -> Dict[str, Optional[str]]:
    """``{node: gpu_source}`` from each node's latest idle row of the last hour (NRG-13)."""
    try:
        idle_rows = await repo.read_idle_metrics(now - GPU_SOURCE_LOOKBACK, now + timedelta(seconds=1))
    except Exception as error:
        logger.warning("Could not read idle rows for the node GPU power sources: %s", error)
        return {}
    latest: Dict[str, Any] = {}
    for row in idle_rows or []:
        node = getattr(row, "node", None)
        timestamp = getattr(row, "timestamp", None)
        if not node or timestamp is None:
            continue
        if node not in latest or timestamp >= latest[node].timestamp:
            latest[node] = row
    return {node: getattr(row, "gpu_source", None) for node, row in latest.items()}


def _with_gpu_source(nodes: List[Any], sources: Dict[str, Optional[str]]) -> List[NodeResponse]:
    responses = []
    for node in nodes:
        fields = node.model_dump(exclude=set(type(node).model_computed_fields))
        responses.append(NodeResponse(**fields, gpu_source=sources.get(node.name)))
    return responses


@router.get("/nodes", response_model=None)
async def list_nodes(
    include_inactive: bool = Query(False, description="Include nodes whose latest state is inactive."),
    limit: Optional[int] = Query(None, ge=1, le=1000, description="Maximum nodes in a cursor page."),
    cursor: Optional[str] = Query(None, description="Opaque cursor from a previous page."),
    repo: NodeRepository = Depends(get_node_repository),
    metrics_repo: CombinedMetricsRepository = Depends(get_combined_metrics_repository),
):
    """Return the latest snapshot of all known Kubernetes nodes, with their GPU power source."""
    now = datetime.now(timezone.utc)
    snapshots = await repo.get_latest_snapshots_before(now, include_inactive=include_inactive)
    nodes = _with_gpu_source(list(snapshots or []), await _latest_gpu_sources(metrics_repo, now))
    if limit is None and cursor is None:
        return nodes
    cursor_name = None
    if cursor:
        try:
            payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            cursor_name = payload["name"]
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as e:
            from fastapi import HTTPException

            raise HTTPException(status_code=400, detail="Invalid pagination cursor.") from e
    ordered = sorted(nodes, key=lambda node: node.name)
    if cursor_name is not None:
        ordered = [node for node in ordered if node.name > cursor_name]
    page = ordered[: limit or 1000]
    next_cursor = None
    if len(ordered) > len(page) and page:
        next_cursor = base64.urlsafe_b64encode(
            json.dumps({"name": page[-1].name}, separators=(",", ":")).encode()
        ).decode()
    return PaginatedNodesResponse(items=page, next_cursor=next_cursor)
