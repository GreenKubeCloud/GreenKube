"""PostgreSQL adapter for durable plan-operation queues."""

from greenkube.storage.base_plan_operation_sql_repository import BaseSQLPlanOperationRepository


class PostgresPlanOperationRepository(BaseSQLPlanOperationRepository):
    backend = "postgres"
