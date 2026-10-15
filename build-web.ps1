# 构建前端（Vue 3 + Naive UI + Tailwind）到 web/dist
#
# 用法：
#   .\build-web.ps1            构建（依赖没装齐时自动 npm install）
#   .\build-web.ps1 -Install   强制重新 npm install
#
# 改完 web/src 下的东西要跑一次，后台服务不用重启（静态文件是每次请求读盘）。
param([switch]$Install)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$web = Join-Path $root 'web'

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Write-Host '没找到 npm。装 Node.js（https://nodejs.org）后再跑这个脚本。' -ForegroundColor Red
    exit 1
}

Set-Location $web
$needInstall = $Install `
    -or -not (Test-Path (Join-Path $web 'node_modules')) `
    -or -not (Test-Path (Join-Path $web 'node_modules\naive-ui'))
if ($needInstall) {
    Write-Host 'npm install …' -ForegroundColor Yellow
    npm install
    if ($LASTEXITCODE -ne 0) {
        Write-Host ''
        Write-Host "npm install 失败（退出码 $LASTEXITCODE），先解决上面的报错。" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

npm run build

# npm 是外部命令，非零退出码不会触发 $ErrorActionPreference='Stop'。
# 不显式检查的话，构建失败了脚本还会打印「已构建到 web\dist」——
# web/dist 里其实还是上一次的旧产物，很容易被当成新版本。
if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Host "构建失败（退出码 $LASTEXITCODE）。" -ForegroundColor Red
    Write-Host 'web\dist 里还是上一次的产物，别当成新版本刷新。' -ForegroundColor Red
    Write-Host '修掉上面的报错再跑一次这个脚本。' -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host ''
Write-Host '前端已构建到 web\dist。刷新浏览器即可（后台不用重启）。' -ForegroundColor Green
