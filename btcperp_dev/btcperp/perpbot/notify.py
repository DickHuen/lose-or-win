"""Desktop notifications on Windows (toast). Best effort: failures are logged, never raised.

Uses PowerShell's built-in WinRT toast API with the PowerShell app id, so nothing extra has to be
installed. Text is passed through environment variables (no string building of user text into the
script). On other operating systems this is a no-op.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Callable

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


def windows_toast(title: str, body: str) -> bool:
    if os.name != "nt":
        return False
    env = dict(os.environ, BTCPERP_TOAST_TITLE=title[:120], BTCPERP_TOAST_BODY=body[:600], BTCPERP_TOAST_APP=_APP_ID)
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _PS],
                           env=env, timeout=20, capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            log.warning("toast failed: %s", r.stderr.decode(errors="replace")[:300])
            return False
        return True
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("toast failed: %s", e)
        return False


def make_notifier(cfg: object) -> Callable[[str, str], bool] | None:
    try:
        enabled = bool(cfg.notifications.windows_toast)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        enabled = False
    if os.environ.get("BTCPERP_NO_TOAST"):          # set by the unit tests (selftest must not pop toasts)
        enabled = False
    if enabled and os.name == "nt":
        return lambda kind, text: windows_toast(f"btcperp: {kind}", text)
    return None
