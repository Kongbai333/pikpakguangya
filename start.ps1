# 一键启动 Web 管理后台（登录 + 群组 + 采集都在这个进程里）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\start.ps1             前台，Ctrl+C 停
#   powershell -ExecutionPolicy Bypass -File .\start.ps1 -NoBrowser  不自动开浏览器
#   powershell -ExecutionPolicy Bypass -File .\start.ps1 -Background 后台常驻，关掉终端也活着
#
# -Background 用 .\stop.ps1 停。
param([switch]$NoBrowser, [switch]$Background)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Write-Host '没找到 .venv。先执行：' -ForegroundColor Yellow
    Write-Host '  py -3 -m venv .venv'
    Write-Host '  .\.venv\Scripts\python.exe -m pip install -r requirements.txt'
    exit 1
}

$cfgPath = Join-Path $root 'config.toml'
if (-not (Test-Path $cfgPath)) {
    Copy-Item (Join-Path $root 'config.example.toml') $cfgPath
    Write-Host '已生成 config.toml（从 config.example.toml 复制）' -ForegroundColor Yellow
}

$cfg = Get-Content -Raw -Path $cfgPath -Encoding UTF8
if ($cfg -match 'api_id\s*=\s*0(\s|$)' -or $cfg -match 'api_hash\s*=\s*""') {
    Write-Host ''
    Write-Host '提示：config.toml 里没填 api_id / api_hash。' -ForegroundColor Yellow
    Write-Host '      如果在管理页「账号 → 应用凭据」里也还没填过，登录会不可用。'
    Write-Host '      申请入口：https://my.telegram.org -> API development tools（免费）。'
    Write-Host ''
}

# 前端产物（web/dist）没有就先构建一次。失败不阻塞启动：会退回旧的 index.html。
$distIndex = Join-Path $root 'web\dist\index.html'
if (-not (Test-Path $distIndex)) {
    if (Get-Command npm -ErrorAction SilentlyContinue) {
        Write-Host '首次启动：正在构建前端（Vue 3 + Naive UI），大概几十秒…' -ForegroundColor Yellow
        try {
            Push-Location (Join-Path $root 'web')
            if (-not (Test-Path (Join-Path $root 'web\node_modules'))) { & npm install }
            & npm run build
            # 外部命令的非零退出码不会抛异常，得自己看
            if ($LASTEXITCODE -ne 0) { throw "npm run build 退出码 $LASTEXITCODE" }
            Pop-Location
            Write-Host '前端构建完成' -ForegroundColor Green
        } catch {
            Pop-Location -ErrorAction SilentlyContinue
            Write-Host "前端构建失败（$($_.Exception.Message)）。" -ForegroundColor Yellow
            Write-Host '会退回旧的 index.html，不是最新的界面。' -ForegroundColor Yellow
            Write-Host '手动重试：.\build-web.ps1' -ForegroundColor Yellow
        }
    } else {
        Write-Host '没找到 npm，前端没构建，会回退到旧的 index.html。' -ForegroundColor Yellow
        Write-Host '装了 Node 之后跑一次：.\build-web.ps1' -ForegroundColor Yellow
    }
}

if ($Background) {
    $logDir = Join-Path $root 'logs'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $p = Start-Process -FilePath $py -ArgumentList '-m', 'app.api' `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logDir 'server.out.log') `
        -RedirectStandardError (Join-Path $logDir 'server.err.log')
    $p.Id | Set-Content -Path (Join-Path $root '.server.pid') -Encoding ascii
    Write-Host "已在后台启动（PID $($p.Id)）" -ForegroundColor Green
    Write-Host '管理后台  http://127.0.0.1:8000/#/admin'
    Write-Host "日志      logs\server.out.log"
    Write-Host '停止      .\stop.ps1' -ForegroundColor DarkGray
    exit 0
}

Write-Host '管理后台  http://127.0.0.1:8000/#/admin' -ForegroundColor Green
Write-Host '展示页    http://127.0.0.1:8000/' -ForegroundColor Green
Write-Host '登录 / 群组 / 采集都在这个进程里；Ctrl+C 停止（采集也一起停）' -ForegroundColor DarkGray
Write-Host '想关掉终端还继续跑：.\start.ps1 -Background' -ForegroundColor DarkGray
Write-Host ''

if (-not $NoBrowser) {
    Start-Job -ScriptBlock {
        Start-Sleep -Seconds 3
        Start-Process 'http://127.0.0.1:8000/#/admin'
    } | Out-Null
}

& $py -m app.api
