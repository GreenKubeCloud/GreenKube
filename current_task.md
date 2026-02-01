# GreenKube Code Analysis Report

**Date:** February 1, 2026  
**Branch:** `fix/code-analysis-issues`  
**Analyzed By:** GitHub Copilot

---

## Summary

After a deep analysis of the entire GreenKube project, I identified **14 issues** ranging from bugs to inconsistencies and potential problems. All 197 tests pass.

### ✅ Fixed Issues

The following issues have been fixed on branch `fix/code-analysis-issues`:

| Commit | Issues Fixed | Description |
|--------|--------------|-------------|
| `34e23bf` | #1 | Remove duplicate return in factory.py |
| `cd36fc5` | #2, #9, #11 | Processor fixes: duplicate log, unused pass, missing close() |
| `2fb2e2a` | #3 | Fix type annotation syntax in cli/utils.py |
| `028677f` | #4, #5, #6, #10 | Persist embodied_co2e_grams in SQLite and PostgreSQL |
| `ee0493a` | #7 | Fix PostgreSQL query execution in EmbodiedRepository |
| `2067e2f` | #12 | Fix Helm chart secret key mismatch |
| `de7f17d` | #14 | Replace lambda with named function for scheduler |

### ⏳ Remaining Issues (Not Fixed)

| Issue | Description | Reason |
|-------|-------------|--------|
| #8 | Missing OpenTelemetry dependencies | Dead code - telemetry module not used yet |
| #13 | RuntimeWarning in tests | Test warnings only, doesn't affect functionality |

---

## Issues Found

### 1. **Duplicate Return Statement in `factory.py`** ✅ FIXED

**File:** `src/greenkube/core/factory.py` (lines 104-105)

**Problem:** The `get_embodied_repository()` function has a duplicate return statement, which means the second return is unreachable dead code.

```python
@lru_cache(maxsize=1)
def get_embodied_repository() -> EmbodiedRepository:
    from ..core.db import db_manager
    return EmbodiedRepository(db_manager)
    return EmbodiedRepository(db_manager)  # <-- DUPLICATE - DEAD CODE
```

**Fix:** Remove the duplicate return statement.

---

### 2. **Duplicate Log Message in `processor.py`** ✅ FIXED

**File:** `src/greenkube/core/processor.py` (lines 624-625)

**Problem:** There's a duplicate logging statement at the end of the `run()` method.

```python
logger.info("Processing complete. Found %d combined metrics.", len(combined_metrics))
logger.info("Processing complete. Found %d combined metrics.", len(combined_metrics))  # <-- DUPLICATE
```

**Fix:** Remove the duplicate log statement.

---

### 3. **Invalid Python Type Annotation Syntax in `utils.py`** ✅ FIXED

**File:** `src/greenkube/cli/utils.py` (line 57)

**Problem:** The return type annotation uses parentheses syntax which is not valid Python syntax for tuple type hints.

```python
def get_normalized_window() -> (datetime, datetime):  # INVALID SYNTAX
```

**Fix:** Change to proper tuple type annotation:
```python
def get_normalized_window() -> tuple[datetime, datetime]:
```

---

### 4. **Missing `embodied_co2e_grams` Column in SQLite Schema** ✅ FIXED

**File:** `src/greenkube/core/db.py` (lines 166-189)

**Problem:** The SQLite `combined_metrics` table creation doesn't include the `embodied_co2e_grams` column, but the PostgreSQL schema does (line 331). There's also no migration to add this column for SQLite (only PostgreSQL has the migration at lines 383-388).

**Fix:** 
1. Add `embodied_co2e_grams REAL,` to the SQLite `combined_metrics` table schema
2. Add a migration block for SQLite:
```python
try:
    await self.connection.execute("ALTER TABLE combined_metrics ADD COLUMN embodied_co2e_grams REAL")
except sqlite3.OperationalError:
    pass
```

---

### 5. **`embodied_co2e_grams` Not Persisted in SQLite Repository** ✅ FIXED

**File:** `src/greenkube/storage/sqlite_repository.py` (lines 163-204)

**Problem:** The `write_combined_metrics` method doesn't include the `embodied_co2e_grams` column in the INSERT statement, and `read_combined_metrics` doesn't read it. This means embodied emissions data is lost when using SQLite.

**Fix:** Add `embodied_co2e_grams` to both the INSERT and SELECT queries, and include it in the parameter tuple.

---

### 6. **`embodied_co2e_grams` Not Persisted in PostgreSQL Repository** ✅ FIXED

**File:** `src/greenkube/storage/postgres_repository.py` (lines 105-175)

**Problem:** Similar to SQLite - the `write_combined_metrics` method doesn't include `embodied_co2e_grams` in the INSERT/UPDATE query, so embodied emissions are calculated but never saved.

**Fix:** Add `embodied_co2e_grams` to the INSERT query, VALUES, and ON CONFLICT UPDATE clauses.

---

### 7. **Inconsistent PostgreSQL Query Execution in `embodied_repository.py`** ✅ FIXED

**File:** `src/greenkube/storage/embodied_repository.py` (lines 61-88)

**Problem:** The `get_profile` method uses `async with conn.execute(query, params) as cursor` pattern which works for SQLite (aiosqlite) but NOT for PostgreSQL (asyncpg). PostgreSQL uses `conn.fetchrow(query, *params)`.

```python
async with conn.execute(query, params) as cursor:  # Works for SQLite, FAILS for PostgreSQL
    row = await cursor.fetchone()
```

**Fix:** Split the logic based on db_type:
```python
if self.db_manager.db_type == "postgres":
    row = await conn.fetchrow(query, provider, instance_type)
else:  # sqlite
    async with conn.execute(query, params) as cursor:
        row = await cursor.fetchone()
```

---

### 8. **Missing OpenTelemetry Dependencies** ⚠️ MISSING DEPENDENCY

**File:** `src/greenkube/core/telemetry.py`

**Problem:** The `telemetry.py` module imports OpenTelemetry packages that are not listed in `pyproject.toml` dependencies:
- `opentelemetry`
- `opentelemetry-sdk`
- `opentelemetry-exporter-otlp-proto-http`

If this module is ever imported, it will cause an ImportError.

**Fix:** Either:
1. Add the dependencies to `pyproject.toml` under `[project.optional-dependencies]`
2. Or remove/disable the `telemetry.py` module if it's not intended for use yet
3. Or wrap imports in try/except with graceful degradation

---

### 9. **Unused `pass` Statement After Logging** ✅ FIXED

**File:** `src/greenkube/core/processor.py` (line 98)

**Problem:** There's a redundant `pass` statement after a successful log message:

```python
if mapped:
    # Direct mapping success
    pass  # <-- UNNECESSARY
    logger.info(...)
```

**Fix:** Remove the `pass` statement as it serves no purpose.

---

### 10. **Dead Code in `postgres_repository.py`** ✅ FIXED

**File:** `src/greenkube/storage/postgres_repository.py` (lines 168-169)

**Problem:** There's a commented debug log that's just a `pass` statement:

```python
if metrics_data:
    # Debug log removed
    pass
```

**Fix:** Remove the empty if block entirely.

---

### 11. **Potential Resource Leak - BoaviztaCollector close() not called** ✅ FIXED

**File:** `src/greenkube/core/processor.py` (lines 629-636)

**Problem:** The `DataProcessor.close()` method doesn't close the `boavizta_collector`. This could lead to unclosed HTTP clients.

```python
async def close(self):
    await self.prometheus_collector.close()
    await self.opencost_collector.close()
    await self.node_collector.close()
    await self.pod_collector.close()
    await self.electricity_maps_collector.close()
    # MISSING: await self.boavizta_collector.close()
```

**Fix:** Add `await self.boavizta_collector.close()` to the close method.

---

### 12. **Helm Chart Secret Key Mismatch** ✅ FIXED

**File:** `helm-chart/templates/deployment.yaml` (line 68)

**Problem:** The environment variable `DB_CONNECTION_STRING` is mapped from a secret key `dbConnectionString`, but in `secret.yaml` it's stored as `DB_CONNECTION_STRING`. This could cause connection string to not be found.

```yaml
# deployment.yaml
valueFrom:
  secretKeyRef:
    name: {{ include "greenkube.fullname" . }}
    key: dbConnectionString  # camelCase

# secret.yaml  
DB_CONNECTION_STRING: {{ ... }}  # SNAKE_CASE
```

**Fix:** Ensure consistent key naming between deployment.yaml and secret.yaml. Use `DB_CONNECTION_STRING` in both places.

---

### 13. **RuntimeWarning in Tests - Coroutines Never Awaited** ⚠️ TEST ISSUE

**Files:** Multiple test files

**Problem:** Test warnings indicate that some async mock calls are not being awaited properly:

```
RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call' was never awaited
```

This occurs in:
- `tests/core/test_processor_estimation_flags.py` (lines 96, 174)
- `tests/core/test_processor_run_range.py`
- `tests/integration/test_prometheus_k8s_integration.py`
- `tests/storage/test_elasticsearch_repository.py`

**Fix:** Ensure all AsyncMock return values are properly awaited in tests, or use `return_value` with proper async mock configuration.

---

### 14. **Scheduler Lambda Function Pattern Issue** ✅ FIXED

**File:** `src/greenkube/cli/start.py` (lines 152-155)

**Problem:** Using a lambda to wrap an async function for the scheduler may cause issues because lambda returns a coroutine but the scheduler might not handle it correctly:

```python
scheduler.add_job_from_string(
    lambda: async_write_combined_metrics_to_database(last=None), 
    config.PROMETHEUS_QUERY_RANGE_STEP
)
```

**Fix:** Use a proper async function instead of lambda:
```python
async def scheduled_write_metrics():
    await async_write_combined_metrics_to_database(last=None)

scheduler.add_job_from_string(scheduled_write_metrics, config.PROMETHEUS_QUERY_RANGE_STEP)
```

---

## Priority Order for Fixes

### High Priority (Bugs affecting functionality):
1. Issue #4, #5, #6 - `embodied_co2e_grams` not saved (data loss)
2. Issue #7 - PostgreSQL query execution failure in embodied_repository
3. Issue #3 - Invalid type annotation (could cause type checking failures)

### Medium Priority (Code quality):
4. Issue #1 - Duplicate return statement
5. Issue #2 - Duplicate log message  
6. Issue #11 - Missing boavizta_collector.close()
7. Issue #12 - Helm secret key mismatch

### Low Priority (Code smell / maintenance):
8. Issue #8 - Missing telemetry dependencies (dead code)
9. Issue #9, #10 - Unnecessary pass statements
10. Issue #13 - Test warnings
11. Issue #14 - Lambda pattern in scheduler

---

## Additional Notes

- All 197 unit tests pass
- Version numbers are consistent across all files (0.1.6)
- Ruff linter shows no issues with the code style
- The codebase follows good practices overall with proper async/await patterns
- Documentation is present and well-structured

