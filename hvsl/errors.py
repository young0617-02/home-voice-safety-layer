"""Typed failures raised by the decision chain."""


class HvslError(Exception):
    """Base class for every error the layer raises on purpose."""


class UnknownToolError(HvslError):
    pass


class ToolValidationError(HvslError):
    pass


class NamespaceViolationError(HvslError):
    """Raised when a tool name is registered without the integration namespace prefix."""


class NoTargetError(HvslError):
    pass


class AmbiguousTargetError(HvslError):
    def __init__(self, message: str, candidates: list[str]) -> None:
        super().__init__(message)
        self.candidates = candidates


class BackendUnavailableError(HvslError):
    pass


class PreDispatchError(HvslError):
    """The backend proved the command was never sent.

    ``retryable=True`` by default keeps the historical meaning ("nothing left the
    process, a retry is safe").  Deterministic policy/state refusals pass
    ``retryable=False``: sending the same command again cannot change the answer,
    so the caller must not advertise it as a retry.
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class DispatchTimeoutError(HvslError):
    pass


class ConfirmationRequiredError(HvslError):
    pass


class ConfirmationExpiredError(HvslError):
    pass


class PolicyError(HvslError):
    pass
