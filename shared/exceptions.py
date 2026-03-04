class NexusError(Exception):
    """Base exception for NexusAI."""


class ConfigError(NexusError):
    """Raised when configuration is invalid or missing."""


class WorkerNotFoundError(NexusError):
    """Raised when a worker cannot be found."""


class BotNotFoundError(NexusError):
    """Raised when a bot cannot be found."""


class TaskNotFoundError(NexusError):
    """Raised when a task cannot be found."""


class SchedulerError(NexusError):
    """Raised when scheduling fails."""


class BackendError(NexusError):
    """Raised when a backend call fails."""


class NoViableBackendError(SchedulerError):
    """Raised when no backend is available for a task."""
