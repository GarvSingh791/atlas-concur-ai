"""ai_agents guardrail exception hierarchy.

Parallel to services/exceptions.py::ProviderError — structurally identical so
callers can handle both layers consistently, but DISTINCT from ProviderError and
from the backend application error hierarchy.  Never import application-layer errors here.

Messages MUST be safe for logging: no secrets, no PII, no raw user data.
"""


class GuardrailError(Exception):
    """Base ai_agents guardrail error.

    Raised when a guardrail detects a configuration or validation problem
    (e.g. out-of-range confidence value, malformed record).  Not a security
    block — see GuardrailBlocked for that.

    Messages must be safe for logging (no secrets, no PII).
    """

    code: str = "GUARDRAIL_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class GuardrailBlocked(GuardrailError):
    """Raised when an agent action is blocked by a permission check (NFR-SEC6).

    Signals that the principal's granted_permission_keys do not include the
    required key for the requested action.  The message MUST NOT expose the
    full permission-key set or the principal's internal state.
    """

    code = "GUARDRAIL_BLOCKED"
