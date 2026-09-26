"""Desktop notifications on Windows (toast). Best effort: failures are logged, never raised.

Uses PowerShell's built-in WinRT toast API with the PowerShell app id, so nothing extra has to be
installed. Text is passed through environment variables (no string building of user text into the
script). On other operating systems this is a no-op.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

log = logging.getLogger("perpbot.notify")

_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
_PS = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$t = [System.Security.SecurityElement]::Escape($env:BTCPERP_TOAST_TITLE)
$b = [System.Security.SecurityElement]::Escape($env:BTCPERP_TOAST_BODY)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$t</text><text>$b</text></binding></visual></toast>")
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:BTCPERP_TOAST_APP).Show($toast)
"""


def windows_toast(title: str, body: str) -> subprocess.Popen[bytes] | None:
    """Start a toast without waiting for it (review v1.2.0 item 6). Returns the process, or None."""
    if os.name != "nt":
        return None
    env = dict(os.environ, BTCPERP_TOAST_TITLE=title[:120], BTCPERP_TOAST_BODY=body[:600], BTCPERP_TOAST_APP=_APP_ID)
    try:
        return subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                 "-Command", _PS], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("toast failed: %s", e)
        return None


class ToastNotifier:
    """Callable notifier: starts toasts immediately, `wait()` lets them finish before the process exits."""

    def __init__(self) -> None:
        self.procs: list[subprocess.Popen[bytes]] = []

    def __call__(self, kind: str, text: str) -> bool:
        proc = windows_toast(f"btcperp: {kind}", text)
        if proc is not None:
            self.procs.append(proc)
        return proc is not None

    def wait(self, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        for proc in self.procs:
            try:
                _, err = proc.communicate(timeout=max(0.1, deadline - time.monotonic()))
                if proc.returncode:
                    log.warning("toast failed: %s", (err or b"").decode(errors="replace")[:300])
            except subprocess.TimeoutExpired:
                log.warning("toast still running after %.0fs; leaving it", timeout)
            except (OSError, ValueError):
                pass
        self.procs = []


def make_notifier(cfg: object) -> ToastNotifier | None:
    try:
        enabled = bool(cfg.notifications.windows_toast)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        enabled = False
    if os.environ.get("BTCPERP_NO_TOAST"):          # set by the unit tests (selftest must not pop toasts)
        enabled = False
    if enabled and os.name == "nt":
        return ToastNotifier()
    return None
