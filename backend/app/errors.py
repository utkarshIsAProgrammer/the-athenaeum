"""A single `{"error": "..."}` envelope for every failure the API can emit."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)


def error_payload(message: str) -> dict[str, str]:
    return {"error": message}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) and exc.detail else "Request failed."
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(detail),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # pydantic reports a list of problems; the client only renders one line,
        # so flatten them into a single readable sentence.
        problems: list[str] = []
        for error in exc.errors():
            location = ".".join(
                str(part) for part in error.get("loc", ()) if part not in {"body", "query"}
            )
            message = str(error.get("msg") or "invalid value")
            problems.append(f"{location}: {message}" if location else message)

        return JSONResponse(
            status_code=422,
            content=error_payload("; ".join(problems) or "Invalid request."),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content=error_payload("Internal server error."))
