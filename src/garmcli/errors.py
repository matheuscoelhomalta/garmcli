"""Safe errors shared by authentication and the read-only adapter."""


class BackendError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)

    def as_dict(self) -> dict:
        result = {"code": self.code, "message": self.message}
        if self.status is not None:
            result["status"] = self.status
        return result
