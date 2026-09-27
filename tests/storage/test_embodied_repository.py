from unittest.mock import MagicMock

from greenkube.core.db import DatabaseManager
from greenkube.storage.embodied_repository import (
    EmbodiedRepository,
    PostgresEmbodiedRepository,
    SQLiteEmbodiedRepository,
)


def test_embodied_repository_selects_postgres_backend():
    mgr = MagicMock(spec=DatabaseManager)
    mgr.db_type = "postgres"

    repo = EmbodiedRepository(mgr)

    assert isinstance(repo._impl, PostgresEmbodiedRepository)


def test_embodied_repository_selects_sqlite_backend_by_default():
    mgr = MagicMock(spec=DatabaseManager)
    mgr.db_type = "sqlite"

    repo = EmbodiedRepository(mgr)

    assert isinstance(repo._impl, SQLiteEmbodiedRepository)
