"""Errors the service raises. The API and MCP map each kind to a response."""


class ServiceError(Exception):
    """A call the service refused. The subclass says why, and the message says what."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Refused(ServiceError):
    """A login, key exchange, or session check failed. It never says which part."""


class NotFound(ServiceError):
    """No such row in the acting user's account. A row in another account is not found."""


class Forbidden(ServiceError):
    """The acting user may not make this call, or this call may not make this write."""


class Invalid(ServiceError):
    """An input fails a check. The message names the input."""


class Conflict(ServiceError):
    """The call does not fit the stored state, such as a taken name or a live key."""
