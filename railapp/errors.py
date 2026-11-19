"""Application level errors with stable machine readable codes."""


class AppError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message, *, status=None, code=None, details=None):
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code
        self.details = details

    def to_dict(self):
        payload = {"error": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return payload


class BadRequest(AppError):
    status = 400
    code = "bad_request"


class NotFound(AppError):
    status = 404
    code = "not_found"


class Conflict(AppError):
    status = 409
    code = "conflict"


class TooLarge(AppError):
    status = 413
    code = "file_too_large"


class Unprocessable(AppError):
    status = 422
    code = "unprocessable"

class Unavailable(AppError):
    status = 503
    code = "unavailable"
