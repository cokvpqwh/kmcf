"""Windows 任务计划：每个工作日收盘后自动生成复盘报告。

任务以当前用户身份运行（只在用户登录时运行，不需要密码）；
错过时间（比如电脑关机）会在下次开机后尽快补跑。
"""

import json
import re
import subprocess
import sys
from pathlib import Path

from . import paths

TASK_NAME = "kmcf_daily_review"
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _pythonw() -> Path:
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return pyw if pyw.exists() else exe


def task_command() -> tuple[str, str]:
    """(可执行文件, 参数)：打包版直接调用 kmcf.exe --scheduled，源码版用 pythonw 运行 run_daily.py。"""
    if getattr(sys, "frozen", False):
        return sys.executable, "--scheduled"
    return str(_pythonw()), '"' + str(paths.ROOT / "run_daily.py") + '"'


def _ps(script: str, timeout: int = 30) -> subprocess.CompletedProcess:
    script = "[Console]::OutputEncoding = [Text.Encoding]::UTF8\n$ErrorActionPreference = 'Stop'\n" + script
    try:
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=_NO_WINDOW,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=f"操作任务计划超时（{timeout} 秒）")


def _check_time(time_str: str) -> str:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", time_str.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"时间格式应为 HH:MM，收到 {time_str!r}")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _q(s) -> str:
    """PowerShell 单引号字符串。"""
    return "'" + str(s).replace("'", "''") + "'"


def install(time_str: str = "19:00") -> str:
    time_str = _check_time(time_str)
    exe, arg = task_command()
    ps = f"""
$a = New-ScheduledTaskAction -Execute {_q(exe)} -Argument {_q(arg)} -WorkingDirectory {_q(paths.ROOT)}
$t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At {_q(time_str)}
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName {_q(TASK_NAME)} -Action $a -Trigger $t -Settings $s -Description 'kmcf A股每日复盘：交易日收盘后自动生成复盘报告' -Force | Out-Null
"""
    r = _ps(ps)
    if r.returncode != 0:
        raise RuntimeError(f"创建定时任务失败：{(r.stderr or r.stdout).strip()}")
    return f"已设置：每个工作日 {time_str} 自动生成复盘（节假日自动跳过）"


def uninstall() -> str:
    r = _ps(f"Unregister-ScheduledTask -TaskName {_q(TASK_NAME)} -Confirm:$false")
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()
        if "No MSFT_ScheduledTask" in err or "找不到" in err:
            return "自动复盘本来就没有开启"
        raise RuntimeError(f"删除定时任务失败：{err}")
    return "已关闭自动复盘"


def status() -> dict:
    """{'installed': bool, 'time': 'HH:MM', 'state': str, 'next_run': str, 'last_run': str, 'last_result': int}"""
    ps = f"""
$t = Get-ScheduledTask -TaskName {_q(TASK_NAME)} -ErrorAction SilentlyContinue
if ($null -eq $t) {{ '{{"installed": false}}' }} else {{
  $i = $t | Get-ScheduledTaskInfo
  $start = [datetime]$t.Triggers[0].StartBoundary
  [ordered]@{{
    installed = $true
    time = $start.ToString('HH:mm')
    state = "$($t.State)"
    next_run = if ($i.NextRunTime) {{ $i.NextRunTime.ToString('yyyy-MM-dd HH:mm') }} else {{ '' }}
    last_run = if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) {{ $i.LastRunTime.ToString('yyyy-MM-dd HH:mm') }} else {{ '' }}
    last_result = $i.LastTaskResult
  }} | ConvertTo-Json -Compress
}}
"""
    r = _ps(ps)
    if r.returncode != 0:
        return {"installed": False, "error": (r.stderr or r.stdout).strip()}
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"installed": False, "error": r.stdout.strip()}
