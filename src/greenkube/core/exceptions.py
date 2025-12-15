class GreenKubeError(Exception):
    """Base exception for GreenKube."""

    pass


class DatabaseError(GreenKubeError):
    """Base exception for database related errors."""

    pass


class ConnectionError(DatabaseError):
    """Raised when database connection fails."""

    pass


class QueryError(DatabaseError):
    """Raised when a database query fails."""

    pass
