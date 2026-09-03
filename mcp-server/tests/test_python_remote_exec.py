"""Tests for PythonRemoteExecClient (UE Python Remote Execution protocol).

Wire-format + timeout tests are deterministic. The full handshake test uses a
fake editor over multicast loopback and is skipped where multicast is unavailable.
"""

import json
import socket
import struct
import threading
import time

import pytest

from unreal_agent_player.errors import AgentError, ErrorCode
from unreal_agent_player.transport import PythonRemoteExecClient


def test_encode_roundtrip():
    c = PythonRemoteExecClient()
    raw = c._encode(c.T_COMMAND, dest="abc", data={"command": "x"})
    msg = json.loads(raw.decode("utf-8"))
    assert msg["magic"] == "ue_py"
    assert msg["version"] == 1
    assert msg["type"] == "command"
    assert msg["dest"] == "abc"
    assert msg["data"] == {"command": "x"}
    assert msg["source"] == c._node_id


def test_decode_rejects_garbage():
    assert PythonRemoteExecClient._decode(b"\xff\xfe not json") is None
    assert PythonRemoteExecClient._decode(b'"a string"') is None  # not a dict
    assert PythonRemoteExecClient._decode(b'{"type":"pong"}') == {"type": "pong"}


def test_exec_python_no_editor_raises():
    # Asserts the no-editor path, so it only means anything when no editor is listening.
    # Discovery is multicast on the whole machine: a developer with an editor open (the
    # normal state for anyone working on this) gets a real pong, the call succeeds, and
    # this fails through no fault of the code -- which then blocks the pre-push hook.
    client = PythonRemoteExecClient(discovery_timeout=0.3)
    try:
        client.exec_python("print('x')")
    except AgentError as err:
        assert err.code == ErrorCode.UE_REMOTE_EXEC_OFF
    else:
        pytest.skip("an Unreal editor is listening on this machine; no-editor path not exercisable")


def _fake_editor(node_id: str, ready: threading.Event, stop: threading.Event):
    """Minimal UE-side: pong to pings, connect back on open_connection, return a result."""
    grp, port = PythonRemoteExecClient.MULTICAST_GROUP, PythonRemoteExecClient.MULTICAST_PORT
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    mreq = struct.pack("4s4s", socket.inet_aton(grp), socket.inet_aton("0.0.0.0"))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    sock.settimeout(0.3)

    def enc(t, dest=None, data=None):
        m = {"version": 1, "magic": "ue_py", "source": node_id, "type": t}
        if dest is not None:
            m["dest"] = dest
        if data is not None:
            m["data"] = data
        return json.dumps(m).encode("utf-8")

    ready.set()
    while not stop.is_set():
        try:
            raw, _addr = sock.recvfrom(8192)
        except TimeoutError:
            continue
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        if msg.get("source") == node_id:
            continue
        t = msg.get("type")
        if t == "ping":
            sock.sendto(enc("pong"), (grp, port))
        elif t == "open_connection":
            d = msg.get("data", {})
            try:
                conn = socket.create_connection((d["command_ip"], d["command_port"]), timeout=2)
                sent = conn.recv(65536)  # the command
                # The client identifies every node before it will talk to it (editor vs a
                # `-game` client of the same project), so a fake editor has to answer that
                # probe too -- a node that will not say what it is never gets selected.
                if b"UAPNODE" in sent:
                    ident = json.dumps({"project": "/fake/Fake.uproject", "role": "editor",
                                        "pid": 4242, "cmdline": "UnrealEditor.exe Fake.uproject"})
                    out = "UAPNODE:" + ident + "\n"
                else:
                    out = "hello\n"
                conn.sendall(enc("command_result", data={
                    "success": True, "result": "None",
                    "output": [{"type": "Info", "output": out}]}))
                conn.close()
            except OSError:
                pass
    sock.close()


def test_full_handshake_with_fake_editor():
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("0.0.0.0", PythonRemoteExecClient.MULTICAST_PORT))
        probe.close()
    except OSError:
        pytest.skip("multicast port unavailable in this environment")

    ready, stop = threading.Event(), threading.Event()
    th = threading.Thread(target=_fake_editor, args=("fake-ue-node", ready, stop), daemon=True)
    th.start()
    ready.wait(2.0)
    time.sleep(0.1)
    try:
        client = PythonRemoteExecClient(discovery_timeout=4.0, exec_timeout=4.0)
        result = client.exec_python("print('hello')")
        assert result["success"] is True
        assert "hello" in result["output"][0]["output"]
    finally:
        stop.set()
        th.join(timeout=2.0)
