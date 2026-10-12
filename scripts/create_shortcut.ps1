<#
.SYNOPSIS
  在桌面创建“kmcf A股复盘.lnk”快捷方式。

.DESCRIPTION
  默认指向绿色版 dist\kmcf\kmcf.exe（工作目录为 exe 所在文件夹，图标用 exe 自带图标）；
  还没有打包时回退为源码版：.venv\Scripts\pythonw.exe main.py（无控制台窗口，图标用 assets\kmcf.ico）。
  也可以用 -Target 指定 kmcf.exe（比如把绿色版文件夹复制到别处之后）或 main.py。
  本脚本只创建快捷方式，不修改任何其他设置；重复运行会覆盖同名快捷方式。

.PARAMETER Target
  快捷方式指向的程序：kmcf.exe 的路径（或它所在的绿色版文件夹），或 main.py 的路径。默认按上面的顺序自动选择。

.PARAMETER Name
  快捷方式名称（不含 .lnk），默认“kmcf A股复盘”。

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1 -Target "D:\Apps\kmcf\kmcf.exe"
#>
param(
    [string]$Target = '',
    [string]$Name = 'kmcf A股复盘'
)

$ErrorActionPreference = 'Stop'

$root    = Split-Path -Parent $PSScriptRoot
$distExe = Join-Path $root 'dist\kmcf\kmcf.exe'
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
$mainPy  = Join-Path $root 'main.py'
$ico     = Join-Path $root 'assets\kmcf.ico'

if (-not $Target) {
    if (Test-Path -LiteralPath $distExe) { $Target = $distExe } else { $Target = $mainPy }
}
# 也可以直接给绿色版文件夹（里面有 kmcf.exe）
if ((Test-Path -LiteralPath $Target -PathType Container) -and (Test-Path -LiteralPath (Join-Path $Target 'kmcf.exe'))) {
    $Target = Join-Path $Target 'kmcf.exe'
}
if (-not (Test-Path -LiteralPath $Target)) {
    Write-Host "没有找到 $Target" -ForegroundColor Red
    Write-Host '请先构建绿色版（powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1），或用 -Target 指定 kmcf.exe / main.py'
    exit 1
}
$Target = (Resolve-Path -LiteralPath $Target).Path

if ($Target -like '*.exe') {
    # 绿色版：直接运行 exe，数据放在 exe 旁边的 data / reports / logs
    $exe       = $Target
    $arguments = ''
    $workDir   = Split-Path -Parent $Target
    $icon      = "$Target,0"
    $kind      = '绿色版'
} elseif ($Target -like '*.py') {
    # 源码版：pythonw 运行 main.py（不显示黑色控制台窗口）
    if (-not (Test-Path -LiteralPath $pythonw)) {
        Write-Host "没有找到 $pythonw" -ForegroundColor Red
        Write-Host '请先在项目目录创建虚拟环境并安装依赖：python -m venv .venv ; .venv\Scripts\pip install -r requirements.txt'
        exit 1
    }
    $exe       = $pythonw
    $arguments = '"' + $Target + '"'
    $workDir   = Split-Path -Parent $Target
    $icon      = $null
    if (Test-Path -LiteralPath $ico) { $icon = "$ico,0" }
    $kind      = '源码版'
} else {
    Write-Host "-Target 只支持 kmcf.exe 或 main.py：$Target" -ForegroundColor Red
    exit 1
}

$desktop = [Environment]::GetFolderPath('Desktop')
$lnkPath = Join-Path $desktop ($Name + '.lnk')

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnkPath)
$sc.TargetPath       = $exe
$sc.Arguments        = $arguments
$sc.WorkingDirectory = $workDir
$sc.Description      = 'kmcf A股每日复盘'
$sc.WindowStyle      = 1
if ($icon) { $sc.IconLocation = $icon }
$sc.Save()

Write-Host "已在桌面创建快捷方式（$kind）：$lnkPath" -ForegroundColor Green
Write-Host "  目标：$exe $arguments"
Write-Host "  起始位置：$workDir"
