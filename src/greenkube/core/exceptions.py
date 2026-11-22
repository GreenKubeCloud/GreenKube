class GreenKubeError(Exception):
    """Base exception for GreenKube."""

    pass


class DatabaseError(GreenKubeError):
    """Base exception for database related errors."""

    pass


class ConnectionError(DatabaseError):
    """Raised when database connection fails."""

    pass


class DatabaseClosedError(DatabaseError):
    """Raised when a closed ``DatabaseManager`` is used: it never reopens on first use (roadmap A.9)."""

    pass


class QueryError(DatabaseError):
    """Raised when a database query fails."""

    pass
