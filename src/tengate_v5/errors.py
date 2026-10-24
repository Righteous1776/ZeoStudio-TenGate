class TenGateError(Exception):
    """Base error for deterministic caller handling."""


class AuthorizationDenied(TenGateError):
    pass


class RevisionConflict(TenGateError):
    pass


class InvalidTransition(TenGateError):
    pass


class IntegrityFailure(TenGateError):
    pass


class UnsupportedCapability(TenGateError):
    pass


class WebhookRejected(TenGateError):
    pass

