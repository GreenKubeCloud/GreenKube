from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from greenkube.core.db import DatabaseManager
from greenkube.storage.embodied_repository import EmbodiedRepository


@pytest.fixture
def mock_db_manager_es():
    mgr = MagicMock(spec=DatabaseManager)
    mgr.db_type = "elasticsearch"
    return mgr


@pytest.fixture
def repo_es(mock_db_manager_es):
    return EmbodiedRepository(mock_db_manager_es)


@pytest.mark.asyncio
async def test_get_profile_es_found(repo_es):
    mock_doc = MagicMock()
    mock_doc.gwp_manufacture = 1000.0
    mock_doc.lifespan_hours = 20000
    mock_doc.source = "test"
    mock_doc.last_updated = "2023-01-01"

    with patch("greenkube.storage.embodied_repository.InstanceCarbonProfileDoc") as MockDoc:
        MockDoc.get = AsyncMock(return_value=mock_doc)

        profile = await repo_es.get_profile("aws", "m5.large")

        assert profile is not None
        assert profile["gwp_manufacture"] == 1000.0
        MockDoc.get.assert_called_with(id="aws-m5.large", ignore=404)


@pytest.mark.asyncio
async def test_get_profile_es_not_found(repo_es):
    with patch("greenkube.storage.embodied_repository.InstanceCarbonProfileDoc") as MockDoc:
        MockDoc.get = AsyncMock(return_value=None)

        profile = await repo_es.get_profile("aws", "unknown")

        assert profile is None


@pytest.mark.asyncio
async def test_save_profile_es(repo_es):
    with patch("greenkube.storage.embodied_repository.InstanceCarbonProfileDoc") as MockDoc:
        mock_instance = MagicMock()
        mock_instance.save = AsyncMock()
        MockDoc.return_value = mock_instance

        await repo_es.save_profile("aws", "m5.large", 1000.0, 20000)

        MockDoc.assert_called()
        args, kwargs = MockDoc.call_args
        assert kwargs["provider"] == "aws"
        assert kwargs["gwp_manufacture"] == 1000.0

        mock_instance.save.assert_called()
