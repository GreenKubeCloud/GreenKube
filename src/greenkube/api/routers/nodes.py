# src/greenkube/api/routers/nodes.py
"""
API routes for Kubernetes node information.
"""

import base64
import binascii
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query

from greenkube.api.dependencies import get_node_repository
from greenkube.api.schemas import PaginatedNodesResponse
from greenkube.storage.base_repository import NodeRepository

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/nodes", response_model=None)
async def list_nodes(
    include_inactive: bool = Query(False, description="Include nodes whose latest state is inactive."),
    limit: Optional[int] = Query(None, ge=1, le=1000, description="Maximum nodes in a cursor page."),
    cursor: Optional[str] = Query(None, description="Opaque cursor from a previous page."),
    repo: NodeRepository = Depends(get_node_repository),
):
    """Return the latest snapshot of all known Kubernetes nodes."""
    now = datetime.now(timezone.utc)
    nodes = await repo.get_latest_snapshots_before(now, include_inactive=include_inactive)
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
