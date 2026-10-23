"""PostgreSQL adapter for durable GitOps state."""

from greenkube.storage.base_gitops_sql_repository import BaseSQLGitOpsRepository


class PostgresGitOpsRepository(BaseSQLGitOpsRepository):
    backend = "postgres"
