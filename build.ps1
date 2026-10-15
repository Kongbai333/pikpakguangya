# 重新生成 data.js（index.html 的离线数据快照）
# 用法： powershell -ExecutionPolicy Bypass -File .\build.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$json = Get-Content -Raw -Path (Join-Path $root 'shuju.json') -Encoding UTF8 | ConvertFrom-Json
if (-not $json.items) { throw 'shuju.json 中未找到 items 数组' }
$payload = [ordered]@{
  __generatedAt = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
  status        = $json.status
  items         = $json.items
}
$out = 'window.__DATA__ = ' + ($payload | ConvertTo-Json -Depth 6 -Compress) + ';' + "`n"
[IO.File]::WriteAllText((Join-Path $root 'data.js'), $out, (New-Object Text.UTF8Encoding($false)))
Write-Host ("data.js 已生成，共 {0} 条记录" -f $json.items.Count)
