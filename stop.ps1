# 停掉后台常驻的服务（start.ps1 -Background 起的那个）
# 用法： powershell -ExecutionPolicy Bypass -File .\stop.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $root '.server.pid'

if (-not (Test-Path $pidFile)) {
    Write-Host '没找到 .server.pid —— 说明不是 -Background 起的。' -ForegroundColor Yellow
    Write-Host '如果是前台运行，在那个窗口按 Ctrl+C 就行。'
    exit 1
}

$target = (Get-Content $pidFile -Raw).Trim()
if (-not $target) {
    Remove-Item $pidFile -Force
    Write-Host '.server.pid 是空的，已清掉。' -ForegroundColor Yellow
    exit 1
}

if (-not (Get-Process -Id $target -ErrorAction SilentlyContinue)) {
    Remove-Item $pidFile -Force
    Write-Host "PID $target 已经不在跑了，清理记录。" -ForegroundColor Yellow
    exit 0
}

# /T 连子进程一起杀，采集跑在同一进程里，会一起收尾
taskkill /T /F /PID $target | Out-Null
Remove-Item $pidFile -Force
Write-Host "已停止（PID $target）" -ForegroundColor Green
