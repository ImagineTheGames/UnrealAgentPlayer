from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    UE_UNREACHABLE = "UE_UNREACHABLE"
    UE_REMOTE_CONTROL_OFF = "UE_REMOTE_CONTROL_OFF"
    UE_REMOTE_EXEC_OFF = "UE_REMOTE_EXEC_OFF"
    UE_CONNECTION_RESET = "UE_CONNECTION_RESET"
    UE_OBJECT_NOT_FOUND = "UE_OBJECT_NOT_FOUND"
    PIE_WRONG_PHASE = "PIE_WRONG_PHASE"
    PIE_TIMEOUT = "PIE_TIMEOUT"
    PIE_CRASHED = "PIE_CRASHED"
    ACTOR_NOT_FOUND = "ACTOR_NOT_FOUND"
    HELPER_UNKNOWN = "HELPER_UNKNOWN"
    HELPER_UNSUPPORTED_ARG = "HELPER_UNSUPPORTED_ARG"
    HELPER_RAISED = "HELPER_RAISED"
    CONSOLE_EXEC_FAILED = "CONSOLE_EXEC_FAILED"
    INPUT_NO_VIEWPORT = "INPUT_NO_VIEWPORT"
    PYTHON_SYNTAX = "PYTHON_SYNTAX"
    PYTHON_RUNTIME = "PYTHON_RUNTIME"
    SCHEMA_VALIDATION = "SCHEMA_VALIDATION"
    TIMEOUT = "TIMEOUT"
    UIA_UNAVAILABLE = "UIA_UNAVAILABLE"
    UIA_PATH_NOT_FOUND = "UIA_PATH_NOT_FOUND"
    INSTANCE_NOT_FOUND = "INSTANCE_NOT_FOUND"
    GAME_LAUNCH_FAILED = "GAME_LAUNCH_FAILED"
    REPORT_NO_SESSION = "REPORT_NO_SESSION"

    @classmethod
    def domain_of(cls, code: ErrorCode) -> str:
        transport = {cls.UE_UNREACHABLE, cls.UE_REMOTE_CONTROL_OFF, cls.UE_REMOTE_EXEC_OFF,
                     cls.UE_CONNECTION_RESET}
        mcp_side = {cls.SCHEMA_VALIDATION, cls.TIMEOUT, cls.UIA_UNAVAILABLE, cls.UIA_PATH_NOT_FOUND}
        if code in transport:
            return "transport"
        if code in mcp_side:
            return "mcp_side"
        return "ue_side"


def ok_response(body: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": True}
    if body is not None:
        out.update(body)
    return out


def error_response(
    code: ErrorCode,
    message: str,
    *,
    recoverable: bool = True,
    retry_hint: str | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": code.value,
            "message": message,
            "domain": ErrorCode.domain_of(code),
            "recoverable": recoverable,
            "retry_hint": retry_hint,
        },
    }


class AgentError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        recoverable: bool = True,
        retry_hint: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.recoverable = recoverable
        self.retry_hint = retry_hint

    def to_response(self) -> dict[str, Any]:
        return error_response(
            self.code, self.message, recoverable=self.recoverable, retry_hint=self.retry_hint
        )
