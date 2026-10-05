"""Tests for PythonRemoteExecClient (UE Python Remote Execution protocol).

Wire-format + timeout tests are deterministic. The handshake tests use a fake editor over
multicast loopback and are skipped where multicast is unavailable.

A WARNING ABOUT THE FAKE EDITOR IN THIS FILE, because it caused a real defect
(ClickUp 17tm466ft7z). Remote-execution discovery is MACHINE-WIDE multicast on UDP 6766.
A fake node here is therefore not confined to this process: unless it filters, it answers
every other process's discovery on the whole box, and its connect-back races the real
editor's. Its canned output then comes back to somebody else's `uap exec` as
`{"ok": true, "output": ["hello\\n"]}`. That is exactly what happened on 2026-09-25, twice,
while five agents shared one editor and this suite ran alongside them.

So every fake in this file answers ONLY nodes in an explicit allowlist of the client ids the
test created. Do not add a fake that talks to anyone who pings.
"""

import json
import socket
import struct
import threading
import time
import uuid

import pytest

from unreal_agent_player.errors import AgentError, ErrorCode
from unreal_agent_player.transport import PythonRemoteExecClient

FAKE_PROJECT = "/fake/Fake.uproject"


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


# --- fake editors ---------------------------------------------------------------------------


def _multicast_available() -> bool:
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("0.0.0.0", PythonRemoteExecClient.MULTICAST_PORT))
        probe.close()
        return True
    except OSError:
        return False


def _new_client(**kw) -> PythonRemoteExecClient:
    """A client with a node id we can put on a fake editor's allowlist."""
    c = PythonRemoteExecClient(**kw)
    c._node_id = "uaptest-" + uuid.uuid4().hex
    return c


class _FakeEditor:
    """A UE-side stand-in that only ever talks to the node ids it was given.

    `honour_dest=False` reproduces the hijack: it answers an `open_connection` that was
    addressed to a DIFFERENT node, which is what lets a stray process return its own output
    as the answer to somebody else's command.

    `delay` holds the REPLY back after the connection is made; `connect_delay` holds back the
    CONNECTION itself. They are not interchangeable: the client accepts connect-backs in the
    order they arrive, so only `connect_delay` decides who is accepted first. A test that needs
    the hijacker to get in first must say so with `connect_delay` on the real node -- `delay`
    alone leaves the connect order to thread scheduling, and on a CI runner the real node won
    that race and the hijack this test exists for never happened.
    """

    def __init__(self, node_id: str, allow: set[str], *, honour_dest: bool = True,
                 output: str | None = None, echo: bool = False, delay: float = 0.0,
                 connect_delay: float = 0.0,
                 project: str = FAKE_PROJECT, serve: bool = True):
        self.node_id = node_id
        self.allow = allow
        self.honour_dest = honour_dest
        self.output = output
        self.echo = echo
        self.delay = delay
        self.connect_delay = connect_delay
        self.project = project
        self.serve = serve
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._th = threading.Thread(target=self._run, daemon=True)
        self._workers: list[threading.Thread] = []

    def __enter__(self):
        self._th.start()
        self._ready.wait(2.0)
        time.sleep(0.1)
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._th.join(timeout=3.0)
        for w in self._workers:
            w.join(timeout=3.0)

    def _enc(self, t, dest=None, data=None):
        m = {"version": 1, "magic": "ue_py", "source": self.node_id, "type": t}
        if dest is not None:
            m["dest"] = dest
        if data is not None:
            m["data"] = data
        return json.dumps(m).encode("utf-8")

    def _run(self):
        grp, port = PythonRemoteExecClient.MULTICAST_GROUP, PythonRemoteExecClient.MULTICAST_PORT
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        mreq = struct.pack("4s4s", socket.inet_aton(grp), socket.inet_aton("0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        sock.settimeout(0.2)
        self._ready.set()
        while not self._stop.is_set():
            try:
                raw, _addr = sock.recvfrom(8192)
            except (TimeoutError, OSError):
                continue
            try:
                msg = json.loads(raw.decode("utf-8"))
            except Exception:
                continue
            src = msg.get("source")
            # THE machine-safety gate. Never answer a process this test did not create.
            if src not in self.allow:
                continue
            t = msg.get("type")
            if t == "ping":
                sock.sendto(self._enc("pong", dest=src), (grp, port))
            elif t == "open_connection":
                if not self.serve:
                    continue                     # still pongs, but answers no commands
                if self.honour_dest and msg.get("dest") not in (None, "", self.node_id):
                    continue
                d = msg.get("data", {})
                w = threading.Thread(target=self._serve, args=(src, d), daemon=True)
                self._workers.append(w)
                w.start()

    def _serve(self, requester: str, d: dict):
        if self.connect_delay:
            time.sleep(self.connect_delay)
        try:
            conn = socket.create_connection((d["command_ip"], d["command_port"]), timeout=3)
        except OSError:
            return
        try:
            sent = conn.recv(65536)
            command = ""
            try:
                command = (json.loads(sent.decode("utf-8")).get("data") or {}).get("command", "")
            except Exception:
                pass
            if "UAPNODE" in command:
                out = "UAPNODE:" + json.dumps(
                    {"project": self.project, "role": "editor", "pid": 4242,
                     "cmdline": "UnrealEditor.exe Fake.uproject"}) + "\n"
            elif self.echo:
                out = command            # echo the caller's own code back as its output
            else:
                out = self.output if self.output is not None else "hello\n"
            if self.delay:
                time.sleep(self.delay)
            conn.sendall(self._enc("command_result", dest=requester, data={
                "success": True, "result": "None", "command": command,
                "output": [{"type": "Info", "output": out}]}))
        except OSError:
            pass
        finally:
            conn.close()


# --- tests ----------------------------------------------------------------------------------


@pytest.mark.skipif(not _multicast_available(), reason="multicast port unavailable")
def test_full_handshake_with_fake_editor():
    client = _new_client(discovery_timeout=4.0, exec_timeout=4.0)
    with _FakeEditor("fake-ue-node", {client._node_id}):
        result = client.exec_python("print('hello')")
    assert result["success"] is True
    assert "hello" in result["output"][0]["output"]


@pytest.mark.skipif(not _multicast_available(), reason="multicast port unavailable")
def test_a_node_that_hijacks_the_connect_back_is_refused_not_returned():
    """ClickUp 17tm466ft7z: the exact defect, reproduced.

    A second node ignores `dest` and connects back to a command port advertised to somebody
    else. Before the fix the client returned the first thing that connected, so this came
    back as `{"ok": true, "output": ["i am not your editor\\n"]}`. Now the foreign reply is
    refused and the real node's answer is the one returned.
    """
    client = _new_client(discovery_timeout=4.0, exec_timeout=4.0,
                         node_project_substr="Fake")
    allow = {client._node_id}
    # connect_delay, not just delay: the hijacker has to be ACCEPTED first for there to be a
    # hijack to refuse. See _FakeEditor.
    with _FakeEditor("real-node", allow, delay=0.30, connect_delay=0.30, echo=True), \
            _FakeEditor("rogue-node", allow, honour_dest=False,
                        output="i am not your editor\n", project="/rogue/Rogue.uproject"):
        result = client.exec_python("print('TOKEN-REAL')")

    got = "".join(o["output"] for o in result["output"])
    assert "i am not your editor" not in got, "a foreign reply was returned as the answer"
    assert "TOKEN-REAL" in got
    assert client.last_rejected, "the hijack happened but was not recorded"
    assert any(r["source"] == "rogue-node" for r in client.last_rejected)


@pytest.mark.skipif(not _multicast_available(), reason="multicast port unavailable")
def test_only_a_hijacker_answering_fails_loudly_rather_than_returning_its_output():
    """When the selected node goes quiet and only a stray one answers, REFUSE.

    This is the half that matters most. The old code returned the first thing that connected
    to its port, so this case produced `ok: true` carrying a completely unrelated process's
    output -- silently, which is how it survived long enough to be filed.
    """
    client = _new_client(discovery_timeout=3.0, exec_timeout=3.0, node_project_substr="Fake")
    client.CONNECTION_RETRIES = 1
    allow = {client._node_id}
    with _FakeEditor("real-node", allow, echo=True) as real, \
            _FakeEditor("rogue-node", allow, honour_dest=False,
                        output="stale answer\n", project="/rogue/Rogue.uproject"):
        client.exec_python("print('warm')")          # prove + cache the real node
        real.serve = False                            # it still pongs; it answers nothing
        with pytest.raises(AgentError) as err:
            client.exec_python("print('TOKEN-SECOND')")
    assert err.value.code is ErrorCode.UE_CROSSED_RESPONSE
    # The refusal has to NAME the process that answered, or the next person to hit this has no
    # more to go on than the last one did.
    assert "rogue-node" in err.value.message
    assert all(r["source"] == "rogue-node" for r in client.last_rejected)


@pytest.mark.skipif(not _multicast_available(), reason="multicast port unavailable")
def test_a_hijacker_cannot_forge_another_nodes_identity():
    """The identity probe is a command exchange too, so it was forgeable the same way.

    `_probe_node` asks a node what project it is and whether it is the editor, and the whole
    "never talk to a node that does not match" guarantee rests on the answer. Because the
    probe's reply was accepted from whoever connected back first, a stray node could answer
    the probe addressed to a different one -- and its project/role would be recorded as that
    other node's. Two earlier incidents (PIE in the wrong project, exec against a `-game`
    client) came from wrong targeting; this made targeting spoofable rather than merely
    fallible.
    """
    client = _new_client(discovery_timeout=4.0, exec_timeout=4.0, node_project_substr="Fake")
    allow = {client._node_id}
    with _FakeEditor("real-node", allow, delay=0.30, connect_delay=0.30, echo=True), \
            _FakeEditor("rogue-node", allow, honour_dest=False,
                        project="/rogue/Rogue.uproject"):
        client.exec_python("print('x')")
    assert client.last_target is not None
    assert client.last_target["node"] == "real-node"
    assert "Rogue" not in str(client.last_target["project"])


@pytest.mark.skipif(not _multicast_available(), reason="multicast port unavailable")
def test_concurrent_callers_each_get_their_own_answer():
    """The stress harness the ticket asks for, in-process.

    N callers, each sending a unique token, against an editor that echoes what it was sent
    and a rogue that hijacks every connect-back it sees. Every response must carry ITS OWN
    token -- not another caller's, and not the rogue's. One clean run proves little on an
    intermittent bug, so this contends repeatedly.
    """
    n, rounds = 6, 3
    clients = [_new_client(discovery_timeout=6.0, exec_timeout=6.0, node_project_substr="Fake")
               for _ in range(n)]
    allow = {c._node_id for c in clients}
    failures: list[str] = []

    def call(c: PythonRemoteExecClient, token: str):
        try:
            res = c.exec_python(f"print('{token}')")
        except AgentError as exc:
            # A refusal is a PASS for this harness: it never hands back a foreign answer.
            if exc.code in (ErrorCode.UE_CROSSED_RESPONSE, ErrorCode.UE_CONNECTION_RESET):
                return
            failures.append(f"{token}: {exc.code.value} {exc.message[:120]}")
            return
        got = "".join(o.get("output", "") for o in (res.get("output") or []))
        if token not in got:
            failures.append(f"{token}: got somebody else's answer -> {got[:120]!r}")

    with _FakeEditor("busy-node", allow, echo=True, delay=0.05), \
            _FakeEditor("rogue-node", allow, honour_dest=False,
                        output="WRONG-ANSWER\n", project="/rogue/Rogue.uproject"):
        for r in range(rounds):
            threads = [threading.Thread(target=call, args=(c, f"TOKEN-{r}-{i}-{uuid.uuid4().hex[:8]}"))
                       for i, c in enumerate(clients)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

    assert not failures, "crossed or wrong answers:\n" + "\n".join(failures)
