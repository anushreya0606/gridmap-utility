"""
excel_open.py - open a file in the desktop Excel of THIS machine at a given sheet / row / column.

Only meaningful when the browser and the server run on the same Windows PC (the usual local
setup: python flask_app.py, browser on 127.0.0.1). Uses Excel's COM interface through
PowerShell, so no extra Python package is needed. The file is opened read-only, so the
input files of a run are never modified; if it is already open, that window is reused.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

_PS = r"""
$ErrorActionPreference = 'Stop'
$path = $env:GM_PATH; $sheet = $env:GM_SHEET; $row = [int]$env:GM_ROW; $col = [int]$env:GM_COL
try { $xl = [Runtime.InteropServices.Marshal]::GetActiveObject('Excel.Application') }
catch { $xl = New-Object -ComObject Excel.Application }
$xl.Visible = $true
if ($xl.WindowState -eq -4140) { $xl.WindowState = -4143 }   # un-minimise
$wb = $null
foreach ($w in $xl.Workbooks) { if ($w.FullName -ieq $path) { $wb = $w } }
if (-not $wb) { $wb = $xl.Workbooks.Open($path, 0, $true) }   # UpdateLinks=0, ReadOnly
if ($sheet) { $ws = $wb.Worksheets.Item($sheet) } else { $ws = $wb.Worksheets.Item(1) }
$wb.Activate(); $ws.Activate()
$win = $xl.ActiveWindow
$first = 1; if ($win.FreezePanes) { $first = $win.SplitRow + 1 }
$win.ScrollRow = [Math]::Max($first, $row - 4)
$win.ScrollColumn = [Math]::Max(1, $(if ($col -gt 6) { $col - 2 } else { 1 }))
[void]$ws.Rows($row).Select()          # highlight the whole source row
[void]$ws.Cells($row, $col).Activate() # active cell = the ID cell
try {
  $p = Get-Process EXCEL | Where-Object { $_.MainWindowHandle -eq [IntPtr]$xl.Hwnd } | Select-Object -First 1
  if ($p) { [void](New-Object -ComObject WScript.Shell).AppActivate($p.Id) }
} catch {}
Write-Output ("ok " + $ws.Name + "!" + $ws.Cells($row, $col).Address($false, $false))
"""


def available() -> bool:
    return os.name == "nt" and shutil.which("powershell") is not None


def open_at(path: Path | str, sheet: str | None, row: int, col: int, timeout: int = 180) -> str:
    """Open `path` in desktop Excel at sheet!(row, col). Returns 'ok Sheet!A1'; raises on failure."""
    env = dict(os.environ, GM_PATH=str(Path(path).resolve()), GM_SHEET=sheet or "",
               GM_ROW=str(int(row)), GM_COL=str(int(col)))
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-Command", _PS], env=env, capture_output=True, text=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = (r.stdout or "").strip().splitlines()
    if r.returncode != 0 or not out or not out[-1].startswith("ok "):
        raise RuntimeError((r.stderr or r.stdout or "Excel did not respond").strip()[-500:])
    return out[-1]
