"""JSON-RPC over HTTPS, plus a small WebSocket client for Solana subscriptions.

PAPER ONLY. This module never signs or sends a transaction.
"""
import base64
import json
import os
import socket
import ssl
import time
import urllib.request
from urllib.parse import urlparse


PUBLIC_HTTP = "https://api.mainnet-beta.solana.com"
PUBLIC_WS = "wss://api.mainnet-beta.solana.com"


def rpc_endpoints():
    """Public mainnet by default. Helius only when HELIUS_API_KEY is set and non-empty."""
    key = (os.environ.get("HELIUS_API_KEY") or "").strip()
    if key:
        return {
            "http": "https://mainnet.helius-rpc.com/?api-key=" + key,
            "ws": "wss://mainnet.helius-rpc.com/?api-key=" + key,
            "name": "helius",
        }
    return {"http": PUBLIC_HTTP, "ws": PUBLIC_WS, "name": "public"}


def rpc_call(url, method, params, timeout=12):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    if isinstance(d, dict) and d.get("error"):
        raise RuntimeError(str(d["error"])[:200])
    return d.get("result") if isinstance(d, dict) else d


def _parse_ws_url(url):
    u = urlparse(url)
    host = u.hostname
    port = u.port or (443 if u.scheme == "wss" else 80)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    return host, port, path, u.scheme == "wss"


def encode_frame(opcode, payload, mask_key=None):
    """Client frame. opcode 1 text, 8 close, 9 ping, 10 pong."""
    if mask_key is None:
        mask_key = os.urandom(4)
    header = bytearray([0x80 | (opcode & 0x0F)])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += n.to_bytes(2, "big")
    else:
        header.append(0x80 | 127)
        header += n.to_bytes(8, "big")
    header += mask_key
    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return bytes(header) + masked


def try_decode_frame(buf):
    """Return (fin, opcode, payload, rest) or None if the buffer is short."""
    if len(buf) < 2:
        return None
    fin = (buf[0] & 0x80) != 0
    opcode = buf[0] & 0x0F
    masked = (buf[1] & 0x80) != 0
    n = buf[1] & 0x7F
    i = 2
    if n == 126:
        if len(buf) < 4:
            return None
        n = int.from_bytes(buf[2:4], "big")
        i = 4
    elif n == 127:
        if len(buf) < 10:
            return None
        n = int.from_bytes(buf[2:10], "big")
        i = 10
    if masked:
        if len(buf) < i + 4:
            return None
        mask = buf[i:i + 4]
        i += 4
    else:
        mask = b""
    if len(buf) < i + n:
        return None
    payload = buf[i:i + n]
    if masked:
        payload = bytes(b ^ mask[j % 4] for j, b in enumerate(payload))
    return fin, opcode, payload, buf[i + n:]


class WsClient:
    """Minimal WebSocket used only for Solana JSON-RPC subscriptions."""

    def __init__(self, url, timeout=20):
        self.url = url
        self.timeout = timeout
        self.sock = None
        self.buf = b""

    def connect(self):
        self.close()
        host, port, path, secure = _parse_ws_url(self.url)
        raw = socket.create_connection((host, port), timeout=self.timeout)
        if secure:
            ctx = ssl.create_default_context()
            self.sock = ctx.wrap_socket(raw, server_hostname=host)
        else:
            self.sock = raw
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("websocket handshake closed")
            resp += chunk
            if len(resp) > 65536:
                raise RuntimeError("websocket handshake too large")
        head, rest = resp.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise RuntimeError("websocket handshake failed: " + status.decode("latin1", "replace")[:160])
        self.buf = rest

    def send_json(self, obj):
        self._send(1, json.dumps(obj).encode())

    def ping(self, payload=b"ping"):
        self._send(9, payload)

    def _send(self, opcode, payload):
        if self.sock is None:
            raise RuntimeError("websocket is closed")
        self.sock.sendall(encode_frame(opcode, payload))

    def recv_json(self, timeout):
        """One text message, or None on timeout. Answers pings. Raises if the peer closes."""
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                return None
            frame = self._read_frame(left)
            if frame is None:
                return None
            opcode, payload = frame
            if opcode == 1:
                return json.loads(payload.decode())
            if opcode == 8:
                raise RuntimeError("websocket closed")
            if opcode == 9:
                self._send(10, payload)

    def _read_exact(self, n, timeout):
        self.sock.settimeout(timeout)
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return False
            if not chunk:
                raise RuntimeError("websocket closed")
            self.buf += chunk
        return True

    def _read_frame(self, timeout):
        deadline = time.time() + timeout
        parts = []
        opcode = None
        while True:
            left = max(0.01, deadline - time.time())
            if time.time() >= deadline:
                return None
            # Pull bytes until one full frame sits in the buffer.
            while True:
                decoded = try_decode_frame(self.buf)
                if decoded is not None:
                    break
                if not self._read_exact(len(self.buf) + 1, left):
                    return None
                left = max(0.01, deadline - time.time())
            fin, op, payload, rest = decoded
            self.buf = rest
            if opcode is None:
                opcode = op
            if op in (0, 1, 2):
                parts.append(payload)
                if fin:
                    return opcode, b"".join(parts)
                continue
            return op, payload

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        self.buf = b""
