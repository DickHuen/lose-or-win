"""v1.5.4: two failures on the owner's Windows PC during the v1.5.3 upgrade.

1. test_local_signing_page_round_trip_and_single_submit: WinError 10053. The signing server answered 403 / 409
   without reading the request body; http.client sends the body in a separate packet, and on Windows a body that
   arrives after the answer resets the connection. Both local servers now read the body first.
2. The automatic restore then failed with WinError 5 removing perpbot\\datasources (a folder held for a moment by
   another program). The restore now overwrites in place and retries every removal.
"""

import io
import json
import socket
import threading
import time
from email.message import Message

import pytest

from perpbot import proxykey as pkm
from perpbot.dashboard import MAX_BODY, read_body

from test_proxykey import FakeExchange, _free_port, _new, root  # noqa: F401  (root: fixture)
from test_review_v130 import _install_mod, _old_install, _zip


def _handler(headers, body):
    msg = Message()
    for k, v in headers.items():
        msg[k] = v
    return type("H", (), {"headers": msg, "rfile": io.BytesIO(body)})()


def test_read_body_reads_the_declared_length_within_the_limit():
    h = _handler({"Content-Length": "5"}, b"abcdefgh")
    assert read_body(h) == b"abcde" and h.rfile.tell() == 5
    assert read_body(_handler({}, b"xyz")) == b""
    assert read_body(_handler({"Content-Length": "nope"}, b"xyz")) == b""
    big = _handler({"Content-Length": str(MAX_BODY * 3)}, b"x" * (MAX_BODY * 3))
    assert len(read_body(big)) == MAX_BODY


def test_signing_server_waits_for_the_body_before_refusing(root, cfg):
    p = _new(root, cfg, method="browser")
    port, token = _free_port(), "t0k3n"
    result = {}
    th = threading.Thread(target=lambda: result.update(pkm.serve_signing(
        root, cfg, p, port=port, open_browser=False, timeout_s=3, transport=FakeExchange().transport(), token=token)))
    th.start()
    s = None
    for _ in range(50):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            break
        except OSError:
            time.sleep(0.1)
    body = json.dumps({"signature": "0x" + "ab" * 65}).encode()
    s.sendall(b"POST /%s/sign HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Token: bad\r\nContent-Length: %d\r\n\r\n"
              % (token.encode(), port, len(body)))
    s.settimeout(0.5)
    with pytest.raises(socket.timeout):                  # no answer while the body is still on its way
        s.recv(1024)
    s.settimeout(5)
    s.sendall(body)
    data = b""
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    s.close()
    assert data.startswith(b"HTTP/1.0 403") and data.endswith(b"forbidden")
    th.join(10)
    assert result.get("ok") is False                      # nothing was signed


def test_restore_retries_a_folder_held_by_another_program(tmp_path, monkeypatch, capsys):
    root = tmp_path / "btcperp"
    _old_install(root)
    (root / "perpbot" / "datasources").mkdir()
    (root / "perpbot" / "datasources" / "binance.py").write_text("OLD = 1\n", encoding="utf-8")
    mod = _install_mod(root)
    monkeypatch.setattr(mod, "RETRY_DELAYS_S", (0.0, 0.0, 0.0))
    monkeypatch.setattr(mod, "existing_tasks", lambda: [])
    real_remove, held = mod._remove, {"n": 0}

    def flaky_remove(p):
        if p.name in ("gone.py", "extra") and held["n"] < 2:     # like WinError 5 on perpbot\datasources
            held["n"] += 1
            raise PermissionError(5, "Access is denied", str(p))
        real_remove(p)

    monkeypatch.setattr(mod, "_remove", flaky_remove)

    def steps(members):
        mod.extract(members)
        return root / "venv" / "python", 1                          # the new version's tests fail

    monkeypatch.setattr(mod, "_install_steps", steps)
    monkeypatch.setattr(mod.subprocess, "check_call", lambda *a, **k: 0)
    extra = {"btcperp/perpbot/datasources/gone.py": "NEW = 1\n", "btcperp/perpbot/extra/x.py": "X = 1\n"}
    monkeypatch.setattr("sys.argv", ["install.py", "--from-zip", str(_zip(tmp_path / "n.zip", "1.4.0", extra=extra))])
    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "retrying" in out and "restored version 1.3.0" in out and "RESTORE FAILED" not in out
    assert held["n"] == 2
    assert (root / "perpbot" / "datasources" / "binance.py").read_text(encoding="utf-8") == "OLD = 1\n"
    assert not (root / "perpbot" / "datasources" / "gone.py").exists() and not (root / "perpbot" / "extra").exists()
    assert not (root / "perpbot" / "new.py").exists() and (root / "perpbot" / "old.py").exists()
    assert (root / ".env").read_text(encoding="utf-8") == "SECRET=keep\n"


def test_restore_gives_up_after_the_retries(tmp_path, monkeypatch):
    mod = _install_mod(tmp_path)
    monkeypatch.setattr(mod, "RETRY_DELAYS_S", (0.0, 0.0))
    calls = []

    def always():
        calls.append(1)
        raise PermissionError(5, "Access is denied")

    with pytest.raises(PermissionError):
        mod.retry_fs(always, "remove x")
    assert len(calls) == 3
    assert mod.retry_fs(lambda: (_ for _ in ()).throw(FileNotFoundError()), "gone") is None
