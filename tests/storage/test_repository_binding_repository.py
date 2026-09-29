from contextlib import asynccontextmanager

import aiosqlite

from greenkube.models.repository_binding import BindingSource, RepositoryBinding, RepositoryBindingQuery
from greenkube.storage.sqlite.repository_binding_repository import SQLiteRepositoryBindingRepository


class _Database:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def connection_scope(self):
        self.connection.row_factory = aiosqlite.Row
        yield self.connection


async def test_sqlite_binding_round_trip() -> None:
    async with aiosqlite.connect(":memory:") as connection:
        await connection.executescript(
            """
            CREATE TABLE repository_bindings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cluster TEXT NOT NULL DEFAULT '', namespace TEXT NOT NULL,
                workload_kind TEXT NOT NULL, workload_name TEXT NOT NULL,
                repo_url TEXT NOT NULL, path TEXT, branch TEXT, source TEXT NOT NULL,
                priority INTEGER NOT NULL, status TEXT NOT NULL, confidence REAL NOT NULL,
                evidence TEXT NOT NULL, discovered_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE (cluster, namespace, workload_kind, workload_name, source, repo_url, path)
            );
            """
        )
        repository = SQLiteRepositoryBindingRepository(_Database(connection))
        binding = RepositoryBinding(
            cluster="prod",
            namespace="production",
            workload_kind="Deployment",
            workload_name="api",
            repo_url="https://example.test/platform.git",
            path="clusters/prod",
            source=BindingSource.ARGOCD,
            priority=300,
        )
        saved = await repository.save_binding(binding)
        loaded = await repository.get_binding(
            RepositoryBindingQuery(
                cluster="prod",
                namespace="production",
                workload_kind="Deployment",
                workload_name="api",
            )
        )
        assert loaded is not None
        assert saved.repo_url == loaded.repo_url
        assert loaded.path == "clusters/prod"
        assert len(await repository.list_bindings("prod")) == 1
