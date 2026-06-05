from __future__ import annotations

from typing import Any

from unreal_agent_player.errors import ErrorCode, error_response


async def exec_python(
    *, rc: Any, py_exec: Any, code: str, context: str = "editor",
) -> dict[str, Any]:
    resp = py_exec.exec_python(code)
    out_lines: list[str] = []
    err_lines: list[str] = []
    for entry in resp.get("output", []) or []:
        if entry.get("type") == "Error":
            err_lines.append(entry.get("output", ""))
        else:
            out_lines.append(entry.get("output", ""))
    stdout = "".join(out_lines)
    stderr = "".join(err_lines)
    if resp.get("result") == "success":
        return {
            "ok": True, "stdout": stdout, "stderr": stderr,
            "result_repr": resp.get("result_repr", ""),
        }
    combined = stderr or stdout
    if "SyntaxError" in combined:
        return error_response(ErrorCode.PYTHON_SYNTAX, combined.strip(), recoverable=False)
    return error_response(
        ErrorCode.PYTHON_RUNTIME, combined.strip() or "python exec failed", recoverable=False,
    )
