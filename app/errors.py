class BridgeError(Exception):
    status_code: int = 500
    err_type: str = "internal_error"
    code: str = "internal_error"

    def __init__(self, message: str, status_code: int = None):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code


class BridgeAuthError(BridgeError):
    status_code = 503
    err_type = "authentication_error"
    code = "gemini_not_authenticated"


class BridgeBusyError(BridgeError):
    status_code = 429
    err_type = "rate_limit_error"
    code = "bridge_busy"


class BridgeTimeoutError(BridgeError):
    status_code = 504
    err_type = "timeout_error"
    code = "gemini_timeout"


class BridgeUpstreamError(BridgeError):
    status_code = 502
    err_type = "upstream_error"
    code = "gemini_upstream_error"


class BridgeQuotaError(BridgeError):
    status_code = 503
    err_type = "quota_error"
    code = "all_accounts_exhausted"


class BridgeDisabledError(BridgeError):
    status_code = 503
    code = "bridge_disabled"
    err_type = "disabled_error"


class BridgeControlError(BridgeError):
    status_code = 409
    code = "bridge_control_conflict"
    err_type = "conflict_error"

