<#
.SYNOPSIS
  构建 kmcf 绿色版（文件夹 + kmcf.exe，双击运行，不需要安装 Python）。

.DESCRIPTION
  1. 用 .venv 里的 PyInstaller 按 kmcf.spec 构建到 build\pyi-dist\kmcf；
  2. robocopy /MIR 同步到 dist\kmcf，但不动 dist\kmcf 下的 data、reports、logs（重新构建不会丢用户数据）；
  3. 首次构建（dist\kmcf\data 不存在）时，把项目里的 data\config.json、data\history.csv、data\cache\
     和 reports\（含 assets\echarts.min.js，不含 _dev_*.html）复制过去，软件一打开就有历史数据和报告；
  4. 检查 dist 里没有 icu*.dll（必须用系统 System32 自带的）、有 libssl/libcrypto（https 依赖）；
  5. 打印 dist\kmcf 总大小。

.PARAMETER Clean
  删除 build\pyi-work 缓存后完整重建（依赖升级后建议使用）。

.PARAMETER Icon
  重新生成 assets\kmcf.ico（默认只在图标不存在时生成）。

.PARAMETER Selftest
  构建完成后运行 dist\kmcf\kmcf.exe --selftest，截图和结果保存在 build\selftest.png / build\selftest.json。

.PARAMETER Dist
  输出目录，默认 dist\kmcf。可以直接指向已经在用的绿色版文件夹（比如 D:\Apps\kmcf）来升级它，
  其中的 data、reports、logs 同样保留。为防误删（同步用 robocopy /MIR，会删除目录里多余的文件），
  目录已存在且非空时，里面必须同时有 kmcf.exe 文件和 _internal 目录，否则拒绝同步（除非加 -Force）。

.PARAMETER NoBuild
  跳过 PyInstaller，直接把上次构建的 build\pyi-dist\kmcf 同步到输出目录。

.PARAMETER Force
  跳过上面的输出目录检查，即使目录非空且不像 kmcf 绿色版也照样 /MIR 同步（目录里除 data、reports、logs
  以外的多余文件会被删除，慎用）。磁盘根目录、项目目录本身及其上级目录无论如何都不允许。

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1 -Clean -Selftest
  powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1 -NoBuild -Dist D:\Apps\kmcf
#>
param(
    [switch]$Clean,
    [switch]$Icon,
    [switch]$Selftest,
    [string]$Dist = '',
    [switch]$NoBuild,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$root    = $PSScriptRoot
$python  = Join-Path $root '.venv\Scripts\python.exe'
$pyi     = Join-Path $root '.venv\Scripts\pyinstaller.exe'
$spec    = Join-Path $root 'kmcf.spec'
$ico     = Join-Path $root 'assets\kmcf.ico'
$work    = Join-Path $root 'build\pyi-work'
$pyiDist = Join-Path $root 'build\pyi-dist'
$built   = Join-Path $pyiDist 'kmcf'
if ($Dist) {
    # 相对路径按当前目录解析（在 Set-Location 之前）
    $dist = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Dist).TrimEnd('\')
} else {
    $dist = Join-Path $root 'dist\kmcf'
}

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "构建失败：$msg" -ForegroundColor Red; exit 1 }

# robocopy /MIR 会删除输出目录里多余的文件：只允许空目录、不存在的目录或已有的 kmcf 绿色版目录
# （同时有 kmcf.exe 文件和 _internal 目录）；-Force 可跳过，但磁盘根目录、项目目录及其上级目录始终拒绝
$distFull = [IO.Path]::GetFullPath($dist + '\').TrimEnd('\')   # 先补 '\'：-Dist D:\ 经上面 TrimEnd 后是 'D:'
$rootFull = [IO.Path]::GetFullPath($root).TrimEnd('\')
if ($distFull -eq [IO.Path]::GetPathRoot($distFull).TrimEnd('\') -or
    ($rootFull + '\').StartsWith($distFull + '\', [StringComparison]::OrdinalIgnoreCase)) {
    Fail "$dist 是磁盘根目录或项目目录（及其上级目录），不能作为输出目录"
}
if ((Test-Path -LiteralPath $dist) -and
    @(Get-ChildItem -LiteralPath $dist -Force -ErrorAction SilentlyContinue).Count -gt 0) {
    $hasExe = Test-Path -LiteralPath (Join-Path $dist 'kmcf.exe') -PathType Leaf
    $hasInternal = Test-Path -LiteralPath (Join-Path $dist '_internal') -PathType Container
    if (-not ($hasExe -and $hasInternal)) {
        $missing = @()
        if (-not $hasExe) { $missing += 'kmcf.exe' }
        if (-not $hasInternal) { $missing += '_internal 目录' }
        $missing = $missing -join '、'
        if ($Force) {
            Write-Host "警告：$dist 非空且不像 kmcf 绿色版目录（缺少 $missing），因 -Force 仍继续；除 data、reports、logs 外的多余文件会被删除" -ForegroundColor Yellow
        } else {
            Fail "$dist 不是空目录，也不是 kmcf 绿色版目录（缺少 $missing）；为防误删，请换一个目录（确认无误可加 -Force）"
        }
    }
}

function Format-Size([double]$bytes) {
    if ($bytes -ge 1GB) { return ('{0:N2} GB' -f ($bytes / 1GB)) }
    return ('{0:N1} MB' -f ($bytes / 1MB))
}

function Get-DirSize([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return 0 }
    $sum = (Get-ChildItem -LiteralPath $path -Recurse -File -Force -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum).Sum
    if ($null -eq $sum) { return 0 }
    return [double]$sum
}

# robocopy 退出码：0~7 为成功（1=有文件复制，2=有多余文件，4=有不匹配……），>=8 为失败
function Invoke-Robocopy([string]$src, [string]$dst, [string[]]$extra) {
    $rcArgs = @($src, $dst) + $extra + @('/R:2', '/W:1', '/NFL', '/NDL', '/NJH', '/NJS', '/NP')
    & robocopy @rcArgs | Out-Host
    $code = $LASTEXITCODE
    if ($code -ge 8) { Fail "robocopy $src -> $dst 失败（退出码 $code）" }
    return $code
}

Set-Location -LiteralPath $root

if (-not (Test-Path -LiteralPath $python)) { Fail "没有找到 $python，请先创建虚拟环境：python -m venv .venv ; .venv\Scripts\pip install -r requirements.txt" }
if (-not (Test-Path -LiteralPath $pyi))    { Fail "没有找到 $pyi，请先安装：.venv\Scripts\pip install pyinstaller" }

# 正在运行的 dist\kmcf\kmcf.exe 会锁住文件，robocopy 同步会失败
$running = @(Get-Process -Name kmcf -ErrorAction SilentlyContinue | Where-Object {
    try { $_.Path -and ($_.Path -like (Join-Path $dist '*')) } catch { $false } })
if ($running.Count -gt 0) { Fail "$dist\kmcf.exe 正在运行（PID $($running.Id -join ', ')），请先关闭软件再构建" }

# 控制台中文 + 子进程 UTF-8
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

if ($NoBuild) {
    if (-not (Test-Path -LiteralPath (Join-Path $built 'kmcf.exe'))) { Fail "没有找到上次构建的 $built\kmcf.exe，请去掉 -NoBuild 重新构建" }
    Step "跳过 PyInstaller，使用上次构建的 $built（$((Get-Item -LiteralPath (Join-Path $built 'kmcf.exe')).LastWriteTime)）"
} else {
    # ------------------------------------------------------------ 1. 图标
    if ($Icon -or -not (Test-Path -LiteralPath $ico)) {
        Step '生成程序图标 assets\kmcf.ico'
        & $python (Join-Path $root 'scripts\make_icon.py') --check
        if ($LASTEXITCODE -ne 0) { Fail '图标生成失败' }
    }

    # ------------------------------------------------------------ 2. PyInstaller
    Step 'PyInstaller 构建（首次约 2~3 分钟）'
    if ($Clean -and (Test-Path -LiteralPath $work)) { Remove-Item -LiteralPath $work -Recurse -Force }
    if (Test-Path -LiteralPath $built) { Remove-Item -LiteralPath $built -Recurse -Force }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $oldPref = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'   # PyInstaller 的日志写在 stderr，不能当成错误
    & $pyi $spec --noconfirm --distpath $pyiDist --workpath $work
    $pyiCode = $LASTEXITCODE
    $ErrorActionPreference = $oldPref
    if ($pyiCode -ne 0) { Fail "PyInstaller 退出码 $pyiCode（详见上面的日志）" }
    if (-not (Test-Path -LiteralPath (Join-Path $built 'kmcf.exe'))) { Fail "没有生成 $built\kmcf.exe" }
    Write-Host ('PyInstaller 用时 {0:N0} 秒' -f $sw.Elapsed.TotalSeconds)
}

# ---------------------------------------------------------------- 3. 同步到 dist\kmcf（保留用户数据）
Step "同步到 $dist（保留 data、reports、logs）"
$firstBuild = -not (Test-Path -LiteralPath (Join-Path $dist 'data'))
New-Item -ItemType Directory -Force -Path $dist | Out-Null
$keep = @((Join-Path $dist 'data'), (Join-Path $dist 'reports'), (Join-Path $dist 'logs'))
# /XD 用完整路径：只保护 dist\kmcf 根下的这三个目录（_internal\akshare\data 之类同名目录照常同步）
$null = Invoke-Robocopy $built $dist (@('/MIR', '/XD') + $keep)

# ---------------------------------------------------------------- 4. 首次构建：带上历史数据和报告
$srcData    = Join-Path $root 'data'
$srcReports = Join-Path $root 'reports'
$dstData    = Join-Path $dist 'data'
$dstReports = Join-Path $dist 'reports'
if ($firstBuild) {
    Step '首次构建：复制配置、历史数据、缓存和已有报告'
    New-Item -ItemType Directory -Force -Path $dstData | Out-Null
    foreach ($f in @('config.json', 'history.csv')) {
        $p = Join-Path $srcData $f
        if ((Test-Path -LiteralPath $p) -and -not (Test-Path -LiteralPath (Join-Path $dstData $f))) {
            Copy-Item -LiteralPath $p -Destination $dstData; Write-Host "  data\$f"
        }
    }
    # /XC /XN /XO：dist 里已经有的同名文件一律不覆盖（比如只删了 data、reports 还在的情况），只补缺少的文件
    if (Test-Path -LiteralPath (Join-Path $srcData 'cache')) {
        $null = Invoke-Robocopy (Join-Path $srcData 'cache') (Join-Path $dstData 'cache') @('/E', '/XC', '/XN', '/XO', '/XF', '*.tmp', '*.lock')
        Write-Host '  data\cache\'
    }
    if (Test-Path -LiteralPath $srcReports) {
        $null = Invoke-Robocopy $srcReports $dstReports @('/E', '/XC', '/XN', '/XO', '/XF', '_dev_*.html', '*.tmp')
        Write-Host '  reports\（含 assets）'
    }
} else {
    Write-Host "已有用户数据（$dstData 存在），不覆盖 data、reports、logs"
}
# 报告离线显示图表需要 reports\assets\echarts.min.js：缺了就从项目里补一份（不覆盖已有文件）
$echarts = Join-Path $srcReports 'assets\echarts.min.js'
$dstEcharts = Join-Path $dstReports 'assets\echarts.min.js'
if ((Test-Path -LiteralPath $echarts) -and -not (Test-Path -LiteralPath $dstEcharts)) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dstEcharts) | Out-Null
    Copy-Item -LiteralPath $echarts -Destination $dstEcharts
    Write-Host '  补充 reports\assets\echarts.min.js'
}

# ---------------------------------------------------------------- 5. 检查
Step '检查打包结果'
$internal = Join-Path $dist '_internal'
$icu = @(Get-ChildItem -LiteralPath $dist -Recurse -File -Filter 'icu*.dll' -ErrorAction SilentlyContinue)
if ($icu.Count -gt 0) { Fail ("dist 里有 ICU 动态库（会导致 Qt 加载失败，应使用 System32 自带的）：`n  " + ($icu.FullName -join "`n  ")) }
Write-Host '  无 icu*.dll（运行时使用 C:\Windows\System32\icuuc.dll）' -ForegroundColor Green
$ssl = @(Get-ChildItem -LiteralPath $internal -File -Filter 'libssl*.dll' -ErrorAction SilentlyContinue)
$crypto = @(Get-ChildItem -LiteralPath $internal -File -Filter 'libcrypto*.dll' -ErrorAction SilentlyContinue)
if ($ssl.Count -eq 0 -or $crypto.Count -eq 0) { Fail '_internal 里缺少 libssl / libcrypto，https 请求会全部失败' }
Write-Host ("  OpenSSL：{0}, {1}" -f $ssl[0].Name, $crypto[0].Name) -ForegroundColor Green
foreach ($need in @('_ssl.pyd', '_hashlib.pyd', 'PySide6\QtWebEngineProcess.exe', 'PySide6\plugins\platforms\qwindows.dll',
                    'PySide6\resources\qtwebengine_resources.pak', 'PySide6\resources\icudtl.dat',
                    'PySide6\translations\qtbase_zh_CN.qm', 'kmcf\templates\report.html.j2',
                    'akshare\data\ths.js', 'py_mini_racer\mini_racer.dll', 'py_mini_racer\icudtl.dat',
                    'curl_cffi\_wrapper.pyd', 'certifi\cacert.pem')) {
    if (-not (Test-Path -LiteralPath (Join-Path $internal $need))) { Fail "缺少 _internal\$need" }
}
if (@(Get-ChildItem -LiteralPath $internal -Recurse -File -Filter 'libcurl*.dll' -ErrorAction SilentlyContinue).Count -eq 0) {
    Fail '缺少 curl_cffi 的 libcurl 动态库（akshare 导入时需要）'
}
Write-Host '  关键文件齐全（WebEngine、平台插件、翻译、模板、akshare 数据、mini_racer、curl_cffi、证书）' -ForegroundColor Green

# ---------------------------------------------------------------- 6. 大小
$total = Get-DirSize $dist
$userBytes = 0
foreach ($d in $keep) { $userBytes += Get-DirSize $d }
Step '完成'
Write-Host "  程序：$dist\kmcf.exe"
Write-Host ("  {0} 总大小：{1}（程序 {2} + 用户数据 {3}）" -f $dist, (Format-Size $total), (Format-Size ($total - $userBytes)), (Format-Size $userBytes)) -ForegroundColor Green
$scArgs = if ($Dist) { " -Target `"$dist\kmcf.exe`"" } else { '' }
Write-Host "  双击 kmcf.exe 运行；创建桌面快捷方式：powershell -NoProfile -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1$scArgs"

# ---------------------------------------------------------------- 7. 可选：界面自检
if ($Selftest) {
    Step '界面自检（kmcf.exe --selftest）'
    $png = Join-Path $root 'build\selftest.png'
    $json = [IO.Path]::ChangeExtension($png, '.json')
    if (Test-Path -LiteralPath $json) { Remove-Item -LiteralPath $json -Force }
    $p = Start-Process -FilePath (Join-Path $dist 'kmcf.exe') -ArgumentList @('--selftest', "`"$png`"") -WorkingDirectory $dist -PassThru -Wait
    if (Test-Path -LiteralPath $json) { Get-Content -LiteralPath $json -Encoding UTF8 | Out-Host }
    if ($p.ExitCode -ne 0) { Fail "自检失败（退出码 $($p.ExitCode)），截图：$png" }
    Write-Host "  自检通过，截图：$png" -ForegroundColor Green
}
exit 0
