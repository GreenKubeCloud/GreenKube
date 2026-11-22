"""The drift monitor reads a closed change's events once to learn it was not superseded (C8)."""

import uuid
from types import SimpleNamespace

import pytest

from greenkube.verify.drift import DriftMonitor, DriftSettings
from greenkube.verify.model import ChangeStateEvent, ChangeStatus, MeasurementState


class _Changes:
    def __init__(self, event_type: str):
        self.event_type = event_type
        self.calls = 0

    async def list_events(self, change_id):
        self.calls += 1
        return [
            ChangeStateEvent(
                change_id=change_id,
                event_type=self.event_type,
                from_state=MeasurementState.NOT_REALIZED,
                to_state=MeasurementState.CLOSED,
            )
        ]


def _monitor(changes):
    service = SimpleNamespace(changes=changes, lifecycle=None, config=None)
    return DriftMonitor(service, settings=DriftSettings())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_closed_change_that_was_not_superseded_is_looked_up_once():
    changes = _Changes("closed")
    monitor = _monitor(changes)
    status = ChangeStatus(change_id=uuid.uuid4().hex, state=MeasurementState.CLOSED)
    assert await monitor._is_superseded(status) is False
    assert await _monitor(changes)._is_superseded(status) is False
    assert changes.calls == 1


@pytest.mark.asyncio
async def test_a_superseded_change_is_still_recognised_from_its_events():
    changes = _Changes("superseded")
    status = ChangeStatus(change_id=uuid.uuid4().hex, state=MeasurementState.CLOSED)
    assert await _monitor(changes)._is_superseded(status) is True
    assert await _monitor(changes)._is_superseded(status) is True
