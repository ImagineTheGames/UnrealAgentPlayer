"""Transports to Unreal Editor.

Two channels:
- RemoteControlClient: UE's Remote Control HTTP API (127.0.0.1:30010 by default).
  Used for structured calls to UFUNCTIONs on the auto-exposed UAPAgentSubsystem.
- PythonRemoteExecClient: UE's Python Remote Execution (UDP discovery + TCP exec).
  Used for arbitrary `import unreal; ...` editor scripting. Fully implemented
  in Task 4.2.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import socket
import struct
import time
import uuid
from typing import Any

import httpx

from unreal_agent_player.errors import AgentError, ErrorCode

# --- proven-node cache (ClickUp 17tm466ft35) ---------------------------------------------------
# Every `uap exec` re-ran the WHOLE discovery handshake from scratch: multicast ping, an 0.8s
# settle window to catch every responder, then a full identity probe -- itself a round-trip that
# executes Python IN the editor -- against each one, before the caller's actual code ran. Measured
# on this workstation with two editors open: ~1.05s of discovery plus two probe round-trips, per
# call, every call.
#
# None of that answers a new question after the first time. The node id is a uuid4 minted by the
# editor's remote-exec module when the process starts, so it identifies that process for its whole
# lifetime. So: remember the node we PROVED matches, and on the next call just ask whether it is
# still there. A cache hit skips the settle window and both probes; a miss (editor restarted ->
# new node id, so the ping goes unanswered) falls through to the full path unchanged.
#
# The safety property this must not weaken is "never talk to a node that does not match" -- two
# real incidents came from a fallback here (PIE started in the WRONG PROJECT; `exec` answered from
# a `-game` client). The cache cannot weaken it: an entry is only ever written after a real probe
# matched, it is keyed by the same project+instance selector that proved it, and it is only used
# when THAT EXACT node id answers and its pid is still alive. A stale entry can only fail to
# match, never mis-match. $UAP_NODE_CACHE=0 disables it.
_NODE_CACHE_OFF = {"0", "false", "no", "off"}
# How long to wait for the known node to say it is still there. It is a localhost round-trip that
# takes single-digit milliseconds when the editor is up; this is the give-up bound, after which
# the full discovery path runs anyway.
_FAST_PING_TIMEOUT = 0.35
_FAST_PING_TRIES = 2


def node_cache_enabled() -> bool:
    raw = os.environ.get("UAP_NODE_CACHE")
    return not (raw is not None and raw.strip().lower() in _NODE_CACHE_OFF)


def _node_cache_dir() -> pathlib.Path:
    root = os.environ.get("UAP_REPORTS_DIR")
    base = pathlib.Path(root) if root else (pathlib.Path.home() / ".uap-reports")
    return base / ".nodes"


def _pid_alive(pid: int) -> bool:
    """Local, cheap liveness check -- a second gate on a cached entry, costing no network."""
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes

            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
                return bool(ok) and code.value == 259      # STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        except Exception:
            return True                                    # cannot check -> let the ping decide
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False
    except Exception:
        return True


SUBSYSTEM_OBJECT_PATH = "/Engine/Transient.UAPAgentSubsystem_0"
DEFAULT_PRESET_NAME = "UAP_Preset"
PRESET_FUNCTION_URL = "/remote/preset/{preset}/function/{func}"


def rc_for_port(port: int, timeout: float = 10.0) -> RemoteControlClient:
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

    # PYI034 wants `Self`, which is typing.Self (3.11+); this package supports 3.10.
    async def __aenter__(self) -> RemoteControlClient:  # noqa: PYI034
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

    # Identifies the node that answers: which project, whether it is the EDITOR or a `-game`
    # standalone client, its pid, and its command line. The project alone is not an identity --
    # `launch_2p_standalone.ps1` starts `UnrealEditor.exe -game` clients of the SAME project,
    # which answer this same discovery and report the same project file path.
    #
    # The body leaves its answer in `_uap_out` and prints NOTHING. It used to `print()` it, and
    # every print in remote-exec Python is a `LogPython:` line in the editor's Output Log and
    # Saved/Logs -- one `UAPNODE:{...}` line per discovery, 235 of them in one day's
    # SchoolsOut.log while several agents worked (ClickUp 17tm466jt8t). It is now read back
    # through the command's RESULT instead: see quiet_expr. The "# UAPNODE" marker stays in the
    # text so a fake node in the tests can recognise the probe.
    NODE_PROBE_BODY = """# UAPNODE identity probe
import unreal, json, os
try:
    _editor = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem) is not None
except Exception:
    _editor = False
try:
    _cmdline = unreal.SystemLibrary.get_command_line()
except Exception:
    _cmdline = ''
_uap_out = json.dumps({'project': unreal.Paths.get_project_file_path(),
                       'role': 'editor' if _editor else 'game',
                       'pid': os.getpid(),
                       'cmdline': _cmdline[:400]})
"""
    # The old, LOGGED form. Used only when a node answered the quiet probe but its result
    # could not be read -- so a node is never undiscoverable because of the quiet path.
    NODE_PROBE = NODE_PROBE_BODY + "print('UAPNODE:' + _uap_out)\n"

    @staticmethod
    def quiet_expr(body: str, var: str = "_uap_out") -> str:
        """A single EXPRESSION that runs `body` and evaluates to the value it left in `var`.

        Sent with exec_mode "EvaluateStatement", which UE runs through RunString with
        Py_eval_input and hands back as the command's `result` (the repr of the value) --
        without writing anything to the Output Log. A `print()` would be a `LogPython:` line on
        every call. `body` runs in a fresh, private namespace, so it also leaves nothing behind
        in the remote-exec globals dict, which is shared by the whole editor session and is
        where a stale UWorld can be rooted (see ClearRemoteExecGlobals in the plugin).
        """
        return f"(lambda _g: (exec({body!r}, _g), _g.get({var!r}))[1])({{}})"

    @staticmethod
    def decode_eval_result(res: dict[str, Any] | None) -> Any:
        """The Python value behind an EvaluateStatement `result` (a repr), or None."""
        raw = (res or {}).get("result")
        if not isinstance(raw, str) or not raw:
            return None
        try:
            return ast.literal_eval(raw)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            return None

    def eval_quiet(self, body: str, var: str = "_uap_out") -> Any:
        """Run `body` on the selected node and return the value it left in `var`, without a
        single line reaching the editor's Output Log. None when the value could not be read."""
        res = self.exec_python(self.quiet_expr(body, var), exec_mode="EvaluateStatement")
        if res.get("success") is False:
            return None
        return self.decode_eval_result(res)

    def __init__(self, discovery_timeout: float = 3.0, exec_timeout: float = 30.0,
                 command_ip: str = "127.0.0.1", node_project_substr: str | None = None,
                 node_instance: str | None = None):
        self._discovery_timeout = discovery_timeout
        self._exec_timeout = exec_timeout
        self._command_ip = command_ip
        # When several editors answer (e.g. two projects open at once), pick the one
        # whose project file path contains this substring. None = any project.
        self._node_project_substr = node_project_substr
        # WHICH INSTANCE of that project. Every verb in this package assumes the editor, so
        # "editor" is the default and the only implicit target: a `-game` standalone client of
        # the same project is a different process with no editor world, and answering from it
        # is a wrong answer, not a degraded one. Other selectors: "pid:<n>", or a substring
        # matched against the node's command line (e.g. "Context_2").
        self._node_instance = (node_instance or "editor").strip()
        self._node_id = uuid.uuid4().hex
        # What the last selection actually resolved to, and everything discovery saw, so a
        # caller can SAY which process answered instead of inferring it from the answer.
        self.last_target: dict[str, Any] | None = None
        self._last_candidates: list[dict[str, Any]] = []
        # The node an EARLIER attempt in this call ran on. A retry that can no longer find it
        # means the target process ended mid-exec -- a different fact from "nothing matched",
        # and the one the caller needs, because every reading taken after it is void.
        self._prior_target: dict[str, Any] | None = None
        # Foreign command_result messages refused by the last exec, newest exchange only.
        # A crossing that is caught has to be VISIBLE, or the next person to hit it has no
        # more evidence than the last one did (ClickUp 17tm466ft7z).
        self.last_rejected: list[dict[str, Any]] = []

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

        The target is the EDITOR of the project named by node_project_substr, unless
        node_instance says otherwise. Both halves matter: the project picks between two open
        projects, node_instance picks between the editor and a `-game` standalone client of
        that same project. Neither ever falls back to "whatever answered" -- a wrong process
        answers plausibly (a `-game` client returns None for every editor subsystem) and that
        reads as a broken feature rather than a wrong target.

        A dropped connection mid-exec (WinError 10054 and friends) is retried with a
        small bounded backoff: the editor sometimes resets the command socket while
        healthy, and losing a whole poll sample to a raw traceback is not acceptable.
        Retries re-run discovery, so the SAME project filter is re-applied every attempt
        -- a retry can never land on a different editor.
        """
        # Per CALL, not per exchange: a crossing caught during the identity probe is still a
        # crossing the caller needs to know happened.
        self.last_rejected = []
        last_exc: AgentError | None = None
        for attempt in range(self.CONNECTION_RETRIES):
            try:
                return self._exec_python_once(code, unattended=unattended, exec_mode=exec_mode)
            except AgentError as exc:
                if exc.code not in (ErrorCode.UE_CONNECTION_RESET,
                                    ErrorCode.UE_CROSSED_RESPONSE):
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
            # FAST PATH: a node we have already PROVED matches this exact selector. One ping to
            # that node id; if it answers, its identity is already established and both the
            # settle window and the identity probes are pure cost. See the cache notes at the
            # top of this module for why this cannot mis-target.
            cached = self._cached_node()
            if cached is not None and self._ping_node(mcast, dest, str(cached["node"])):
                self._prior_target = self.last_target
                self.last_target = cached
                self._last_candidates = [cached]
                result = self._run_on_node(mcast, dest, str(cached["node"]), code,
                                           unattended, exec_mode)
                if result is not None:
                    return result
                # It answered discovery but would not run anything: stop trusting the entry and
                # fall through to the full path, which will re-probe and re-cache.
                self._forget_node()
            # The target's PONG can be slow/dropped on a given round, so re-discover a few
            # times rather than failing -- and NEVER fall back to a node that does not match
            # (see _select_node).
            attempts = 3
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
                self._forget_node()
                raise self._no_match_error(nodes)
            self._remember_node(self.last_target)
            result = self._run_on_node(mcast, dest, target, code, unattended, exec_mode)
            if result is None:
                raise AgentError(ErrorCode.UE_REMOTE_EXEC_OFF,
                                 "No command_result returned from the editor.")
            return result
        finally:
            mcast.close()

    # --- proven-node cache ---

    def _cache_key(self) -> str:
        raw = f"{self._node_project_substr or '_any'}|{self._node_instance}"
        return re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower() or "default"

    def _cache_file(self) -> pathlib.Path:
        return _node_cache_dir() / f"{self._cache_key()}.json"

    def _cached_node(self) -> dict[str, Any] | None:
        """A previously PROVEN node for this exact selector, or None.

        Re-checks the recorded identity against the live selector before returning it, so an
        entry written under a different filter can never be honoured here, and drops it if the
        process is gone.
        """
        if not node_cache_enabled():
            return None
        try:
            info = json.loads(self._cache_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(info, dict) or not info.get("node"):
            return None
        if not self._matches_project(info) or not self._matches_instance(info):
            return None                       # belt and braces: the filter must still hold
        if not _pid_alive(int(info.get("pid") or 0)):
            self._forget_node()
            return None
        return info

    def _remember_node(self, info: dict[str, Any] | None) -> None:
        if not node_cache_enabled() or not info or info.get("probe_failed"):
            return
        try:
            f = self._cache_file()
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(info), encoding="utf-8")
        except OSError:
            pass                              # a cache that cannot be written is only slow

    def _forget_node(self) -> None:
        try:
            self._cache_file().unlink()
        except OSError:
            pass

    def _ping_node(self, mcast: socket.socket, dest: tuple[str, int], node: str) -> bool:
        """Is that exact node still answering? Returns on ITS pong, never on anyone else's."""
        mcast.settimeout(0.1)
        for _ in range(_FAST_PING_TRIES):
            mcast.sendto(self._encode(self.T_PING), dest)
            deadline = time.monotonic() + _FAST_PING_TIMEOUT
            while time.monotonic() < deadline:
                try:
                    raw, _addr = mcast.recvfrom(8192)
                except (TimeoutError, OSError):
                    continue
                msg = self._decode(raw)
                if msg and msg.get("type") == self.T_PONG and str(msg.get("source")) == node:
                    return True
        return False

    # --- internals ---

    def _discover_nodes(self, mcast: socket.socket, dest: tuple[str, int]) -> list[str]:
        # ALWAYS catch every responder, so _select_node can pick the right one -- and, when
        # nothing matches, NAME the ones that answered. "First responder wins" used to be the
        # no-filter shortcut; it is gone, because the first responder is as likely to be a
        # `-game` standalone client as the editor. We do not burn the full timeout: after the
        # first PONG, wait a short settle window for the others, then stop.
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
            except TimeoutError:
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
            if seen and first_at is not None and (time.monotonic() - first_at) >= settle:
                break
        return seen

    def list_nodes(self) -> list[dict[str, Any]]:
        """Every Unreal node answering discovery right now, with its identity.

        Unfiltered on purpose: this is the verb you run when a call went to the wrong place,
        so it has to show the ones that would NOT be selected too.
        """
        mcast = self._make_multicast_socket()
        dest = (self.MULTICAST_GROUP, self.MULTICAST_PORT)
        try:
            out: list[dict[str, Any]] = []
            for n in self._discover_nodes(mcast, dest):
                out.append(self._probe_node(mcast, dest, n)
                           or {"node": n, "project": None, "role": "unknown", "pid": None,
                               "cmdline": "", "probe_failed": True})
            return out
        finally:
            mcast.close()

    def _select_node(self, mcast: socket.socket, dest: tuple[str, int],
                     nodes: list[str]) -> str | None:
        """The one node matching BOTH the project filter and the instance selector, or None.

        NEVER falls back to a non-matching node -- not even when it is the only one that
        answered this discovery round. Two separate incidents came from a fallback here: the
        intended editor's PONG was slow and PIE started in the WRONG PROJECT; and later, with
        the project pinned, `exec` landed on a `-game` standalone client of the SAME project
        and answered `None` for every editor subsystem, which read as a broken editor.

        Every node is probed for its identity (project, role, pid, command line) so a refusal
        can NAME what it found instead of saying "nothing matched".
        """
        if self.last_target is not None:
            self._prior_target = self.last_target
        self.last_target = None
        self._last_candidates = []
        for n in nodes:
            info = self._probe_node(mcast, dest, n)
            if info is None:
                # A node that will not describe itself is not a node we will silently talk to.
                info = {"node": n, "project": None, "role": "unknown", "pid": None,
                        "cmdline": "", "probe_failed": True}
            self._last_candidates.append(info)
        for info in self._last_candidates:
            if info.get("probe_failed"):
                continue
            if not self._matches_project(info) or not self._matches_instance(info):
                continue
            self.last_target = info
            return str(info["node"])
        return None

    def _probe_node(self, mcast: socket.socket, dest: tuple[str, int],
                    node: str) -> dict[str, Any] | None:
        """Ask `node` who it is -- quietly first, so discovery writes nothing to its log."""
        res = self._run_on_node(mcast, dest, node, self.quiet_expr(self.NODE_PROBE_BODY),
                                True, "EvaluateStatement")
        if not res:
            return None          # did not answer at all: the logged form would not either
        info = self._parse_node_info(self.decode_eval_result(res), node)
        if info is not None:
            return info
        # It answered, but the quiet result could not be read. Fall back to the logged probe
        # rather than leave a live node undiscoverable; this is the only path that prints.
        res = self._run_on_node(mcast, dest, node, self.NODE_PROBE, True, "ExecuteFile")
        if not res:
            return None
        return self._parse_probe_output(res, node)

    @staticmethod
    def _parse_node_info(value: Any, node: str) -> dict[str, Any] | None:
        if not isinstance(value, str):
            return None
        try:
            info = json.loads(value)
        except json.JSONDecodeError:
            return None
        if not isinstance(info, dict):
            return None
        info["node"] = node
        info.setdefault("role", "unknown")
        return info

    @staticmethod
    def _parse_probe_output(res: dict[str, Any], node: str) -> dict[str, Any] | None:
        lines = [o.get("output", "") for o in (res.get("output") or [])]
        lines.append(str(res.get("result") or ""))
        for line in lines:
            if "UAPNODE:" not in line:
                continue
            try:
                info = json.loads(line.split("UAPNODE:", 1)[1].strip())
            except json.JSONDecodeError:
                continue
            if isinstance(info, dict):
                info["node"] = node
                info.setdefault("role", "unknown")
                return info
        return None

    def _matches_project(self, info: dict[str, Any]) -> bool:
        if not self._node_project_substr:
            return True
        return self._node_project_substr.lower() in str(info.get("project") or "").lower()

    def _matches_instance(self, info: dict[str, Any]) -> bool:
        sel = self._node_instance
        if sel.lower() == "editor":
            return info.get("role") == "editor"
        if sel.lower().startswith("pid:"):
            try:
                return int(sel.split(":", 1)[1]) == int(info.get("pid"))
            except (TypeError, ValueError):
                return False
        hay = f"{info.get('cmdline') or ''} {info.get('project') or ''}".lower()
        return sel.lower() in hay

    @staticmethod
    def describe_node(info: dict[str, Any]) -> str:
        project = str(info.get("project") or "?").replace("\\", "/").rsplit("/", 1)[-1]
        cmdline = " ".join(str(info.get("cmdline") or "").split())
        if len(cmdline) > 140:
            cmdline = cmdline[:140] + "..."
        text = f"pid {info.get('pid')} role={info.get('role')} project={project}"
        if info.get("probe_failed"):
            text += " (did not answer the identity probe)"
        return f"{text} cmdline={cmdline!r}" if cmdline else text

    def _no_match_error(self, nodes: list[str]) -> AgentError:
        """The refusal for "something answered, but not the thing you asked for".

        Names every process that DID answer. The failure this replaces returned a confident
        empty answer from the wrong process; a refusal that cannot say what it found is only
        marginally better, because the reader still has to guess.
        """
        seen = self._last_candidates or [{"node": n, "role": "unknown"} for n in nodes]
        listing = "; ".join(self.describe_node(i) for i in seen)
        prior = self._prior_target
        if prior and not any(i.get("pid") == prior.get("pid") for i in seen):
            # It was there, we ran on it, and now it is not. Say THAT.
            return AgentError(
                ErrorCode.UE_WRONG_INSTANCE,
                f"The process this call was running on has gone: "
                f"{self.describe_node(prior)}. It stopped answering mid-exec, so anything the "
                f"call had already done there is unfinished and any reading taken after it is "
                f"not evidence. Still answering: {listing or '(nothing)'}.",
                recoverable=False,
            )
        want = (f"project '{self._node_project_substr}'"
                if self._node_project_substr else "any project")
        if self._node_instance.lower() == "editor":
            same_project = [i for i in seen
                            if self._matches_project(i) and not i.get("probe_failed")]
            if same_project and all(i.get("role") != "editor" for i in same_project):
                return AgentError(
                    ErrorCode.UE_WRONG_INSTANCE,
                    f"No EDITOR answered for {want}, but {len(same_project)} non-editor "
                    f"instance(s) of it did: {listing}. A `-game` standalone client has no "
                    f"editor world and no editor subsystems, so running this against it would "
                    f"return None for everything rather than fail. Start the editor, or target "
                    f"the client deliberately with --instance (e.g. --instance Context_2 or "
                    f"--instance pid:{same_project[0].get('pid')}).",
                    recoverable=False,
                )
        return AgentError(
            ErrorCode.UE_WRONG_INSTANCE,
            f"{len(nodes)} Unreal node(s) answered but none matched {want} + "
            f"instance '{self._node_instance}'. Answered: {listing}.",
            recoverable=False,
        )

    def _run_on_node(self, mcast: socket.socket, dest: tuple[str, int], node: str,
                     code: str, unattended: bool, exec_mode: str) -> dict[str, Any] | None:
        cmd_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cmd_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        cmd_server.bind(("0.0.0.0", 0))
        cmd_server.listen(1)
        cmd_server.settimeout(2.0)
        cmd_port = cmd_server.getsockname()[1]
        # Foreign replies refused during this exchange. Kept so a caller -- and the stress
        # harness -- can SEE that a crossing happened and was caught, not infer it.
        rejected: list[dict[str, Any]] = []
        connected = False
        try:
            # The open_connection datagram or the editor's connect-back can be dropped;
            # resend and re-accept a few times before giving up.
            for _attempt in range(4):
                mcast.sendto(self._encode(
                    self.T_OPEN_CONNECTION, dest=node,
                    data={"command_ip": self._command_ip, "command_port": cmd_port}), dest)
                while True:
                    try:
                        conn, _ = cmd_server.accept()
                    except TimeoutError:
                        break
                    except OSError:
                        break
                    connected = True
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
                        data, reject = self._read_command_result(conn, node)
                    if data is not None:
                        self.last_rejected.extend(rejected)
                        return data
                    if reject is not None:
                        # Somebody else's process answered our port. Do NOT return it, and do
                        # NOT give up: the node we addressed may still be connecting back, so
                        # keep accepting on the same advertised port. We deliberately do not
                        # tear down here -- close_connection would tell the real editor to drop
                        # the very connection we are still waiting for.
                        rejected.append(reject)
                        continue
                    break   # clean EOF with no result -> re-advertise and try again
            self.last_rejected.extend(rejected)
            if not connected:
                raise AgentError(
                    ErrorCode.UE_REMOTE_EXEC_OFF,
                    "Editor did not connect back to the command server.",
                )
            if rejected:
                raise self._crossed_response_error(node, rejected)
            return None
        finally:
            try:
                mcast.sendto(self._encode(self.T_CLOSE_CONNECTION, dest=node), dest)
            except OSError:
                pass  # best-effort teardown; never mask the real error
            cmd_server.close()

    def _crossed_response_error(self, node: str, rejected: list[dict[str, Any]]) -> AgentError:
        """The refusal for -- something answered, but it was not the process I asked."""
        who = "; ".join(
            "source={} dest={}{}".format(
                r.get("source") or "(none)", r.get("dest") or "(none)",
                " output={!r}".format(r["output"]) if r.get("output") else "")
            for r in rejected[:3])
        return AgentError(
            ErrorCode.UE_CROSSED_RESPONSE,
            f"{len(rejected)} foreign reply/replies answered this call's command connection "
            f"instead of the node it was addressed to ({node}), and were refused: {who}. "
            f"Nothing was read from them, so no value here came from the wrong process. "
            f"Something else on this machine answers Python remote-execution and connected "
            f"back to our advertised port first -- another editor, a -game client, or the "
            f"uap test suite's fake editor in tests/test_python_remote_exec.py.",
            retry_hint="transient; retry, and check what else answers with `uap nodes`",
        )

    def _read_command_result(self, conn: socket.socket,
                             expect_source: str) -> tuple[dict[str, Any] | None,
                                                          dict[str, Any] | None]:
        """Read one command_result, returning it ONLY if it answers OUR command.

        Returns (data, None) for our answer and (None, reject) for somebody else's. A reject
        must never be treated as a result.

        The wire protocol already carries the correlation in both directions and this used to
        check neither end of it. Every message we send carries a per-client uuid4 `source`;
        the editor stamps its reply's `source` with its own node id and its `dest` with the
        node id of whoever sent the command -- engine PythonScriptRemoteExecution.cpp:620,
        SendCommandResultMessage(InMessage.Source, ...).

        Checking it matters because "the first process that connected to my port" is NOT the
        same fact as "the editor I selected". open_connection is a MULTICAST datagram, so
        every remote-exec node on the box sees it; the real editor honours its dest
        (PassesReceiveFilter, same file) but nothing on the wire forces that, and the
        connect-back is an ordinary TCP connection to an advertised port. A node that ignores
        dest -- which is exactly what this repo's own fake editor did -- hijacks every
        exchange on the machine and answers success: true with its own canned output. That is
        how `uap exec` returned another caller's "hello" on 2026-09-25, while several agents
        shared one editor and the test suite ran alongside them (ClickUp 17tm466ft7z).

        dest is enforced only when present: source alone already identifies the answering
        process, and refusing a reply that omits an optional field would trade a silent wrong
        answer for a noisy wrong failure.
        """
        buf = b""
        while True:
            try:
                chunk = conn.recv(65536)
            except TimeoutError:
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
            if msg is None:
                continue
            if msg.get("type") != self.T_COMMAND_RESULT:
                buf = b""  # unexpected complete message; keep reading
                continue
            src = str(msg.get("source") or "")
            dst = str(msg.get("dest") or "")
            if src == expect_source and (not dst or dst == self._node_id):
                return msg.get("data", {}), None
            data = msg.get("data") or {}
            outs = [str(o.get("output", "")) for o in (data.get("output") or [])
                    if isinstance(o, dict)]
            return None, {
                "source": src, "dest": dst,
                "expected_source": expect_source, "expected_dest": self._node_id,
                "command": str(data.get("command") or "")[:200],
                "output": ("".join(outs))[:200],
            }
        return None, None
