"""Client side of the line-delimited JSON protocol spoken by bridge/diana_bridge.py."""
from __future__ import annotations

import json
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Any


class BridgeError(RuntimeError):
    """The bridge could not be reached or refused the request."""


class BridgeUnavailable(BridgeError):
    """Nothing is listening on the bridge port (DianaIE closed or the bridge stopped)."""


class BridgeTimeout(BridgeError):
    """No reply in time; DianaIE may still be busy with the request."""


@dataclass(frozen=True)
class BridgeEndpoint:
    host: str
    port: int
    token: str


def request(endpoint: BridgeEndpoint, op: str, timeout: float, **fields: Any) -> dict[str, Any]:
    """Send one request and wait up to ``timeout`` seconds for the reply.

    The request carries a deadline so the bridge drops it if it only gets to it after we gave up:
    connections queue in the OS backlog while the DianaIE main thread is busy.
    """
    payload = {"token": endpoint.token, "id": uuid.uuid4().hex, "op": op, "deadline": time.time() + timeout, **fields}
    try:
        with socket.create_connection((endpoint.host, endpoint.port), timeout=min(timeout, 5)) as sock:
            sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
            sock.settimeout(timeout)
            buf = bytearray()
            while not buf.endswith(b"\n"):
                chunk = sock.recv(1 << 20)
                if not chunk:
                    break
                buf += chunk
    except ConnectionRefusedError as exc:
        raise BridgeUnavailable(f"no DIANA bridge listening on {endpoint.host}:{endpoint.port}") from exc
    except socket.timeout as exc:
        raise BridgeTimeout(f"DianaIE did not reply within {timeout:g} s") from exc
    except OSError as exc:
        raise BridgeError(f"bridge connection failed: {exc}") from exc

    if not buf:
        raise BridgeError("the bridge closed the connection without replying")
    response = json.loads(buf.decode("utf-8"))
    if response.get("error") in ("invalid token", "malformed request"):
        raise BridgeError(f"bridge rejected the request: {response['error']}")
    return response
