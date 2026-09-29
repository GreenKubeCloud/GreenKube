from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from greenkube.core.db import DatabaseManager


class UnitOfWork:
    """Own one database transaction and release its handle deterministically."""

    def __init__(self, manager: DatabaseManager):
        self.manager = manager
        self.connection: Any | None = None
        self._transaction: Any | None = None
        self._owns_connection = False

    async def __aenter__(self) -> "UnitOfWork":
        if self.connection is not None:
            raise RuntimeError("UnitOfWork cannot be entered twice")
        if self.manager.db_type == "postgres":
            await self.manager.ensure_connection()
            assert self.manager.pool is not None
            self.connection = await self.manager.pool.acquire()
            self._owns_connection = True
            self._transaction = self.connection.transaction()
            await self._transaction.start()
        else:
            await self.manager.ensure_connection()
            self.connection = self.manager.connection
            assert self.connection is not None
            await self.connection.execute("BEGIN")
            self._owns_connection = False
        return self

    async def commit(self) -> None:
        if self.connection is None:
            raise RuntimeError("UnitOfWork is not active")
        if self.manager.db_type == "postgres":
            assert self._transaction is not None
            await self._transaction.commit()
            self._transaction = None
        else:
            await self.connection.commit()

    async def rollback(self) -> None:
        if self.connection is None:
            return
        if self.manager.db_type == "postgres":
            if self._transaction is not None:
                await self._transaction.rollback()
                self._transaction = None
        else:
            await self.connection.rollback()

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        try:
            if exc_type is None:
                await self.commit()
            else:
                await self.rollback()
        finally:
            if self._owns_connection and self.connection is not None:
                assert self.manager.pool is not None
                await self.manager.pool.release(self.connection)
            self.connection = None
            self._transaction = None
            self._owns_connection = False

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Any]:
        async with self as unit_of_work:
            assert unit_of_work.connection is not None
            yield unit_of_work.connection
