"""Consistent error responses.

Shape (additive to FastAPI's default, so existing clients that read ``detail`` keep working):
    {"detail": <str or list>, "error_code": "<CODE>", "request_id": "<id>"}
Validation errors never echo submitted values.
"""
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from observability import request_id_var

logger = logging.getLogger("tuloy.errors")

STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    501: "NOT_IMPLEMENTED",
    503: "SERVICE_UNAVAILABLE",
}


class ApiError(HTTPException):
    def __init__(self, status_code: int, error_code: str, detail: str, headers: dict | None = None,
                 extra: dict | None = None):
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.error_code = error_code
        # Additional body fields; they never replace detail, error_code or request_id.
        self.extra = extra


def error_body(status_code: int, detail, error_code: str | None = None) -> dict:
    return {
        "detail": detail,
        "error_code": error_code or STATUS_CODES.get(status_code, "ERROR"),
        "request_id": request_id_var.get(),
    }


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        code = getattr(exc, "error_code", None)
        body = error_body(exc.status_code, exc.detail, code)
        for key, value in (getattr(exc, "extra", None) or {}).items():
            body.setdefault(key, value)
        return JSONResponse(body, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        detail = [
            {"loc": list(err.get("loc", ())), "msg": err.get("msg", ""), "type": err.get("type", "")}
            for err in exc.errors()
        ]
        return JSONResponse(error_body(422, detail), status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        logger.error("unhandled_exception", exc_info=exc)
        return JSONResponse(error_body(500, "Internal server error"), status_code=500)
