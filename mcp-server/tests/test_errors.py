from unreal_agent_player.errors import AgentError, ErrorCode, error_response, ok_response


def test_ok_response_shape():
    assert ok_response({"phase": "Playing"}) == {"ok": True, "phase": "Playing"}


def test_error_response_shape():
    resp = error_response(
        ErrorCode.PIE_WRONG_PHASE,
        "needs Playing",
        recoverable=True,
        retry_hint="call pie_start",
    )
    assert resp == {
        "ok": False,
        "error": {
            "code": "PIE_WRONG_PHASE",
            "message": "needs Playing",
            "domain": "ue_side",
            "recoverable": True,
            "retry_hint": "call pie_start",
        },
    }


def test_agent_error_raises_and_converts():
    err = AgentError(ErrorCode.UE_UNREACHABLE, "no editor")
    assert err.to_response() == {
        "ok": False,
        "error": {
            "code": "UE_UNREACHABLE",
            "message": "no editor",
            "domain": "transport",
            "recoverable": True,
            "retry_hint": None,
        },
    }


def test_ok_response_empty_body_is_preserved():
    # empty dict must not be silently dropped
    assert ok_response({}) == {"ok": True}


def test_error_code_domains_cover_all():
    for code in ErrorCode:
        assert ErrorCode.domain_of(code) in ("transport", "ue_side", "mcp_side")
