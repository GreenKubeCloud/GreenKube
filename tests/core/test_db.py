import sqlite3

import pytest

from greenkube.core.db import DatabaseManager
from greenkube.storage.unit_of_work import UnitOfWork


@pytest.mark.asyncio
async def test_close_resets_handles_and_allows_reconnect(tmp_path):
    manager = DatabaseManager()
    path = str(tmp_path / "database.db")

    await manager.setup_sqlite(path)
    first = manager.connection
    await manager.close()

    assert manager.connection is None
    assert manager.pool is None

    await manager.setup_sqlite(path)
    assert manager.connection is not None
    assert manager.connection is not first
    await manager.close()


@pytest.mark.asyncio
async def test_unit_of_work_commits_and_rolls_back(tmp_path):
    manager = DatabaseManager()
    path = str(tmp_path / "database.db")
    await manager.setup_sqlite(path)

    async with UnitOfWork(manager) as unit_of_work:
        assert unit_of_work.connection is not None
        await unit_of_work.connection.execute("CREATE TABLE values_table (value TEXT)")
        await unit_of_work.connection.execute("INSERT INTO values_table VALUES ('committed')")

    with pytest.raises(RuntimeError):
        async with UnitOfWork(manager) as unit_of_work:
            assert unit_of_work.connection is not None
            await unit_of_work.connection.execute("INSERT INTO values_table VALUES ('rolled back')")
            raise RuntimeError("force rollback")

    await manager.close()
    connection = sqlite3.connect(path)
    values = [row[0] for row in connection.execute("SELECT value FROM values_table")]
    connection.close()
    assert values == ["committed"]


@pytest.mark.asyncio
async def test_use_after_close_raises_instead_of_reopening(tmp_path):
    """Roadmap A.9: a closed manager never reopens a connection on first use."""
    from greenkube.core.config import Config
    from greenkube.core.exceptions import DatabaseClosedError

    manager = DatabaseManager(Config(DB_TYPE="sqlite", DB_PATH=str(tmp_path / "closed.db")))
    await manager.connect()
    await manager.close()

    with pytest.raises(DatabaseClosedError):
        async with manager.connection_scope():
            pass
    with pytest.raises(DatabaseClosedError):
        async with manager.transaction():
            pass
    with pytest.raises(DatabaseClosedError):
        await manager.schema_status()
    assert manager.connection is None

    # An explicit connect or setup opens it again.
    await manager.connect()
    async with manager.connection_scope() as connection:
        async with connection.execute("SELECT 1") as cursor:
            assert (await cursor.fetchone())[0] == 1
    await manager.close()
