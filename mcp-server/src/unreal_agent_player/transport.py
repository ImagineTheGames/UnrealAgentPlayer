"""Transports to Unreal Editor.

Two channels:
- RemoteControlClient: UE's Remote Control HTTP API (127.0.0.1:30010 by default).
  Used for structured calls to UFUNCTIONs on the auto-exposed UAPAgentSubsystem.
- PythonRemoteExecClient: UE's Python Remote Execution (UDP discovery + TCP exec).
  Used for arbitrary `import unreal; ...` editor scripting. Fully implemented
  in Task 4.2.
"""

from __future__ import annotations

import json
import socket
import struct
import time
import uuid
from typing import Any

import httpx

from unreal_agent_player.errors import AgentError, ErrorCode

SUBSYSTEM_OBJECT_PATH = "/Engine/Transient.UAPAgentSubsystem_0"
DEFAULT_PRESET_NAME = "UAP_Preset"
PRESET_FUNCTION_URL = "/remote/preset/{preset}/function/{func}"


def rc_for_port(port: int, timeout: float = 10.0) -> "RemoteControlClient":
    return RemoteControlClient(port=port, timeout=timeout)


class RemoteControlClient:
    """Async client for Remote Control HTTP API."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 30010,
        timeout: float = 10.0,
    ):
        self._base = f"http://{host}:{port}"
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "RemoteControlClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def call_function(
        self,
        object_path: str,
        function_name: str,
        *,
        parameters: dict[str, Any] | None = None,
        generate_transaction: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "objectPath": object_path,
            "functionName": function_name,
            "parameters": parameters or {},
            "generateTransaction": generate_transaction,
        }
        try:
            resp = await self._client.put(f"{self._base}/remote/object/call", json=payload)
        except httpx.ConnectError as exc:
            raise AgentError(
                ErrorCode.UE_UNREACHABLE,
                f"Could not reach Remote Control at {self._base}: {exc}",
                retry_hint="start UE editor and enable Remote Control HTTP server",
            ) from exc
        except httpx.HTTPError as exc:
            raise AgentError(
                ErrorCode.UE_UNREACHABLE, f"HTTP error talking to Remote Control: {exc}"
            ) from exc
        if resp.status_code == 404:
            raise AgentError(
                ErrorCode.UE_OBJECT_NOT_FOUND,
                f"Remote Control 404: {resp.text[:200]}",
                recoverable=False,
            )
        if resp.status_code >= 400:
            raise AgentError(
                ErrorCode.UE_UNREACHABLE,
                f"Remote Control returned {resp.status_code}: {resp.text[:200]}",
                recoverable=False,
            )
        try:
            return resp.json()
        except json.JSONDecodeError:
            return {}

    async def call_preset(
        self,
        function_name: str,
        parameters: dict[str, Any] | None = None,
        *,
        preset: str = DEFAULT_PRESET_NAME,
    ) -> Any:
        """Call a UFUNCTION via the stable embedded-preset endpoint.

        Returns the unwrapped ReturnValue (preset calls wrap results in
        ReturnedValues[0].ReturnValue), or {} when there is no return value.
        """
        path = PRESET_FUNCTION_URL.format(preset=preset, func=function_name)
        try:
            resp = await self._client.put(f"{self._base}{path}", json={"Parameters": parameters or {}})
        except httpx.ConnectError as exc:
            raise AgentError(
                ErrorCode.UE_UNREACHABLE,
                f"Could not reach Remote Control at {self._base}: {exc}",
                retry_hint="start UE editor and enable Remote Control HTTP server",
            ) from exc
        except httpx.HTTPError as exc:
            raise AgentError(
                ErrorCode.UE_UNREACHABLE, f"HTTP error talking to Remote Control: {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise AgentError(
                ErrorCode.UE_UNREACHABLE,
                f"Remote Control preset call returned {resp.status_code}: {resp.text[:200]}",
                recoverable=False,
            )
        try:
            body = resp.json()
        except json.JSONDecodeError:
            return {}
        vals = body.get("ReturnedValues") or []
        if vals and isinstance(vals[0], dict) and "ReturnValue" in vals[0]:
            return vals[0]["ReturnValue"]
        return body

    async def exec_console(self, command: str) -> str:
        """Execute a console command through the stable preset endpoint."""
        result = await self.call_preset("ExecuteConsoleCommand", {"Command": command})
        # Console commands typically have no return value; non-string/empty -> "".
        return str(result if isinstance(result, str) else "")


class PythonRemoteExecClient:
    """Client for UE's Python Remote Execution wire protocol.

    Implements the handshake UE actually uses (Epic's remote_execution.py):
      1. We join the UDP multicast group and broadcast a `ping`.
      2. The editor replies with `pong` carrying its node id.
      3. We open a local TCP "command server" and broadcast `open_connection`
         advertising its address; the editor connects back to us.
      4. We send a `command` over that TCP socket and read the `command_result`.
      5. We broadcast `close_connection`.

    Messages are bare JSON (UTF-8), one object per datagram / per TCP exchange --
    no length prefix, no null framing.
    """

    MULTICAST_GROUP = "239.0.0.1"
    MULTICAST_PORT = 6766
    MULTICAST_TTL = 0
    PROTOCOL_VERSION = 1
    PROTOCOL_MAGIC = "ue_py"

    T_PING = "ping"
    T_PONG = "pong"
    T_OPEN_CONNECTION = "open_connection"
    T_CLOSE_CONNECTION = "close_connection"
    T_COMMAND = "command"
    T_COMMAND_RESULT = "command_result"

    # A live editor occasionally resets the command socket mid-exec. That is transient --
    # the next call succeeds -- so retry a couple of times with a short backoff instead of
    # surfacing a raw ConnectionResetError traceback and losing the sample.
    CONNECTION_RETRIES = 3
    RETRY_BACKOFF = (0.25, 0.75)

    def __init__(self, discovery_timeout: float = 3.0, exec_timeout: float = 30.0,
                 command_ip: str = "127.0.0.1", node_project_substr: str | None = None):
        self._discovery_timeout = discovery_timeout
        self._exec_timeout = exec_timeout
        self._command_ip = command_ip
        # When several editors answer (e.g. two projects open at once), pick the one
        # whose project file path contains this substring. None = first responder.
        self._node_project_substr = node_project_substr
        self._node_id = uuid.uuid4().hex

    # --- message helpers ---

    def _encode(self, msg_type: str, dest: str | None = None, data: Any = None) -> bytes:
        msg = {
            "version": self.PROTOCOL_VERSION,
            "magic": self.PROTOCOL_MAGIC,
            "source": self._node_id,
            "type": msg_type,
        }
        if dest is not None:
            msg["dest"] = dest
        if data is not None:
            msg["data"] = data
        return json.dumps(msg).encode("utf-8")

    @staticmethod
    def _decode(raw: bytes) -> dict[str, Any] | None:
        try:
            msg = json.loads(raw.decode("utf-8"))
            return msg if isinstance(msg, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _make_multicast_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.MULTICAST_PORT))
        mreq = struct.pack("4s4s", socket.inet_aton(self.MULTICAST_GROUP),
                           socket.inet_aton("0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self.MULTICAST_TTL)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        return sock

    # --- public API ---

    def exec_python(self, code: str, *, unattended: bool = True,
                    exec_mode: str = "ExecuteFile") -> dict[str, Any]:
        """Run Python in the editor and return {result, success, output:[...]}.

        If multiple editors answer (e.g. two projects open at once) and
        node_project_substr is set, the editor whose project file path contains
        that substring is chosen; otherwise the first responder is used.

        A dropped connection mid-exec (WinError 10054 and friends) is retried with a
        small bounded backoff: the editor sometimes resets the command socket while
        healthy, and losing a whole poll sample to a raw traceback is not acceptable.
        Retries re-run discovery, so the SAME project filter is re-applied every attempt
        -- a retry can never land on a different editor.
        """
        last_exc: AgentError | None = None
        for attempt in range(self.CONNECTION_RETRIES):
            try:
                return self._exec_python_once(code, unattended=unattended, exec_mode=exec_mode)
            except AgentError as exc:
                if exc.code is not ErrorCode.UE_CONNECTION_RESET:
                    raise
                last_exc = exc
                if attempt < self.CONNECTION_RETRIES - 1:
                    time.sleep(self.RETRY_BACKOFF[attempt])
        assert last_exc is not None
        raise last_exc

    def _exec_python_once(self, code: str, *, unattended: bool,
                          exec_mode: str) -> dict[str, Any]:
        try:
            mcast = self._make_multicast_socket()
        except OSError as exc:
            raise AgentError(
                ErrorCode.UE_REMOTE_EXEC_OFF,
                f"Could not open multicast discovery socket: {exc}",
                retry_hint="Project Settings > Python > Enable Remote Execution",
            ) from exc
        dest = (self.MULTICAST_GROUP, self.MULTICAST_PORT)
        try:
            # When a project filter is set, the target editor's PONG can be slow/dropped on a
            # given round. Re-discover a few times rather than failing -- and NEVER fall back to
            # a non-matching editor (see _select_node).
            attempts = 3 if self._node_project_substr else 1
            nodes: list[str] = []
            target: str | None = None
            for _ in range(attempts):
                nodes = self._discover_nodes(mcast, dest)
                if not nodes:
                    continue
                target = self._select_node(mcast, dest, nodes)
                if target is not None:
                    break
            if not nodes:
                raise AgentError(
                    ErrorCode.UE_REMOTE_EXEC_OFF,
                    "No Unreal node answered Python Remote Execution ping.",
                    retry_hint="Project Settings > Python > Enable Remote Execution, then restart the editor",
                )
            if target is None:
                raise AgentError(
                    ErrorCode.UE_REMOTE_EXEC_OFF,
                    f"{len(nodes)} editor(s) answered but none matched project "
                    f"'{self._node_project_substr}'. Is that editor running?",
                )
            result = self._run_on_node(mcast, dest, target, code, unattended, exec_mode)
            if result is None:
                raise AgentError(ErrorCode.UE_REMOTE_EXEC_OFF,
                                 "No command_result returned from the editor.")
            return result
        finally:
            mcast.close()

    # --- internals ---

    def _discover_nodes(self, mcast: socket.socket, dest: tuple[str, int]) -> list[str]:
        # With a project filter set we must catch ALL responders (so _select_node can pick the
        # right project), but we don't want to burn the full timeout every call. After the first
        # PONG, wait a short settle window for other editors, then stop. With no filter, the
        # first responder is enough.
        deadline = time.monotonic() + self._discovery_timeout
        settle = 0.8
        next_ping = 0.0
        seen: list[str] = []
        first_at: float | None = None
        mcast.settimeout(0.3)
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_ping:
                mcast.sendto(self._encode(self.T_PING), dest)
                next_ping = now + 0.5
            try:
                raw, _ = mcast.recvfrom(8192)
            except socket.timeout:
                if seen and first_at is not None and (time.monotonic() - first_at) >= settle:
                    break
                continue
            except OSError:
                # Windows surfaces an ICMP port-unreachable from a previous datagram as a
                # reset on the NEXT recvfrom of a UDP socket. Not fatal to discovery: keep
                # pinging until the deadline.
                continue
            msg = self._decode(raw)
            if msg and msg.get("type") == self.T_PONG and msg.get("source"):
                src = str(msg["source"])
                if src not in seen:
                    seen.append(src)
                    if first_at is None:
                        first_at = time.monotonic()
                    if not self._node_project_substr:
                        break  # no filter: first responder is enough
            if (self._node_project_substr and seen and first_at is not None
                    and (time.monotonic() - first_at) >= settle):
                break
        return seen

    def _select_node(self, mcast: socket.socket, dest: tuple[str, int],
                     nodes: list[str]) -> str | None:
        if not self._node_project_substr:
            return nodes[0]
        # A project filter is set: NEVER fall back to a non-matching editor -- not even when
        # it is the only one that answered this discovery round. (Doing so cross-targeted the
        # wrong editor when the intended one's UDP PONG was simply slow/dropped, e.g. starting
        # PIE in the wrong project.) Verify each node's project and return ONLY a real match.
        for n in nodes:
            res = self._run_on_node(
                mcast, dest, n,
                "import unreal\nprint(unreal.Paths.get_project_file_path())",
                True, "ExecuteFile")
            text = ""
            if res:
                text = str(res.get("result") or "") + " ".join(
                    o.get("output", "") for o in res.get("output", []))
            if self._node_project_substr.lower() in text.lower():
                return n
        return None

    def _run_on_node(self, mcast: socket.socket, dest: tuple[str, int], node: str,
                     code: str, unattended: bool, exec_mode: str) -> dict[str, Any] | None:
        cmd_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cmd_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        cmd_server.bind(("0.0.0.0", 0))
        cmd_server.listen(1)
        cmd_server.settimeout(2.0)
        cmd_port = cmd_server.getsockname()[1]
        try:
            # The open_connection datagram or the editor's connect-back can be dropped;
            # resend and re-accept a few times before giving up.
            conn = None
            for _attempt in range(4):
                mcast.sendto(self._encode(
                    self.T_OPEN_CONNECTION, dest=node,
                    data={"command_ip": self._command_ip, "command_port": cmd_port}), dest)
                try:
                    conn, _ = cmd_server.accept()
                    break
                except socket.timeout:
                    continue
                except OSError:
                    continue
            if conn is None:
                raise AgentError(
                    ErrorCode.UE_REMOTE_EXEC_OFF,
                    "Editor did not connect back to the command server.",
                )
            try:
                with conn:
                    conn.settimeout(self._exec_timeout)
                    try:
                        conn.sendall(self._encode(
                            self.T_COMMAND, dest=node,
                            data={"command": code, "unattended": unattended,
                                  "exec_mode": exec_mode}))
                    except OSError as exc:
                        raise AgentError(
                            ErrorCode.UE_CONNECTION_RESET,
                            f"Editor closed the command connection while sending: {exc}",
                            retry_hint="transient; retry the call",
                        ) from exc
                    return self._read_command_result(conn)
            finally:
                try:
                    mcast.sendto(self._encode(self.T_CLOSE_CONNECTION, dest=node), dest)
                except OSError:
                    pass  # best-effort teardown; never mask the real error
        finally:
            cmd_server.close()

    def _read_command_result(self, conn: socket.socket) -> dict[str, Any] | None:
        buf = b""
        while True:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                break
            except OSError as exc:
                # WinError 10054 / ECONNRESET: the editor dropped the command socket mid-read.
                # Seen against a healthy PIE session -- the next call works -- so report it as a
                # distinguishable, retryable transport error rather than letting a raw
                # ConnectionResetError traceback escape and kill the caller's poll loop.
                raise AgentError(
                    ErrorCode.UE_CONNECTION_RESET,
                    f"Editor reset the command connection mid-exec: {exc}",
                    retry_hint="transient; retry the call",
                ) from exc
            if not chunk:
                break
            buf += chunk
            msg = self._decode(buf)
            if msg is not None:
                if msg.get("type") == self.T_COMMAND_RESULT:
                    return msg.get("data", {})
                buf = b""  # unexpected complete message; keep reading
        return None
