"""Framework error hierarchy.

Every error carries a stable machine-readable ``code`` so that adapters, agents,
and operator tooling can react to the meaning of a failure rather than its text
(baseline section 4, "Common semantics").
"""

from __future__ import annotations

from typing import Any


class FrameworkError(Exception):
    """Base class for all framework errors."""

    code = "framework_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}

    def __str__(self) -> str:
        if not self.details:
            return self.message
        extras = ", ".join(f"{k}={v!r}" for k, v in self.details.items())
        return f"{self.message} ({extras})"


class SchemaError(FrameworkError):
    """A document does not match its declared contract."""

    code = "schema_error"


class IncompatibleSchemaVersion(SchemaError):
    code = "incompatible_schema_version"


class UnitError(FrameworkError):
    code = "unit_error"


class FrameError(FrameworkError):
    code = "frame_error"


class PluginError(FrameworkError):
    code = "plugin_error"


class UnsupportedOperation(FrameworkError):
    """A skill or operation is not supported by the target; never silently ignored."""

    code = "unsupported_operation"


class AdmissionRejected(FrameworkError):
    """The runtime refused to accept a command. ``reason`` is a stable sub-code."""

    code = "admission_rejected"

    def __init__(self, reason: str, message: str, **details: Any) -> None:
        super().__init__(message, reason=reason, **details)
        self.reason = reason


class AuthorityError(FrameworkError):
    code = "authority_error"


class ResourceConflict(FrameworkError):
    code = "resource_conflict"


class InvalidTransition(FrameworkError):
    code = "invalid_transition"


class OutcomeNotVerified(FrameworkError):
    """Raised by ``wait_for_verified_result`` when execution did not reach VERIFIED."""

    code = "outcome_not_verified"

    def __init__(self, message: str, result: Any, **details: Any) -> None:
        super().__init__(message, **details)
        self.result = result


class PlanError(FrameworkError):
    """An agent produced a plan that does not satisfy the plan contract."""

    code = "plan_error"
