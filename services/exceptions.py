"""Services-layer provider exception hierarchy.

These exceptions are raised by provider adapters when a call cannot be completed.
Backend and AI agent layers translate these into AppError subclasses.
Never import AppError here — keep this module independent of application layers.
"""


class ProviderError(Exception):
    """Base services-layer provider error.

    Raised by provider adapters when a call cannot be completed.
    Messages must be safe for logging (no secrets, keys, or PII).
    """

    code: str = "PROVIDER_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class ProviderConfigurationError(ProviderError):
    """Raised when a real provider adapter is called without required credentials/config.

    Raised AT CALL TIME — never at import time. Keeps hermetic CI importable.
    """

    code = "PROVIDER_CONFIGURATION_ERROR"


class ProviderUnavailableError(ProviderError):
    """Raised when the provider SDK is not installed or the service is unreachable."""

    code = "PROVIDER_UNAVAILABLE"
