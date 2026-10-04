# cn-llm-router 本地网关一键启动（Windows PowerShell）
# 作用：清理可能干扰的环境变量 → 启动 serve（含单实例守护）→ 启动 litellm 桥接。
#
# 用法（仓库根目录）：
#   powershell -ExecutionPolicy Bypass -File scripts\start-gateway.ps1
# 停止：
#   停掉本脚本对应的两个进程即可（或直接关终端）；再次运行本脚本会自动重启。
#
# 为什么清 DATABASE_URL：litellm proxy 在 config 未配置 database_url 时会回退读取
# 环境变量 DATABASE_URL（12-factor 通用名，机器上可能被其他软件占用，如 PostgreSQL 安装）。
# 若指向连不上的数据库，litellm 启动即崩溃。本脚本只对当前进程清理，不改动机器级环境。

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
$Litellm = Join-Path $Root ".venv\Scripts\litellm.exe"

# 1) 清理当前进程环境（litellm 会回退读 DATABASE_URL）
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue

# 2) 启动 serve 网关（单实例守护：端口已有本应用实例会自动关旧启新）
Write-Host "==> 启动 cn-llm-router serve (127.0.0.1:10041)"
$Serve = Start-Process -FilePath $VenvPy -ArgumentList "-m", "cn_llm_router", "serve", "--port", "10041" `
    -RedirectStandardOutput "$Root\serve.out.log" -RedirectStandardError "$Root\serve.err.log" `
    -WindowStyle Hidden -PassThru
Write-Host "    serve PID: $($Serve.Id)（日志：serve.out.log / serve.err.log）"

# 3) 启动 litellm 桥接（Claude Code 上游）
Write-Host "==> 启动 litellm proxy (127.0.0.1:4000)"
$Lit = Start-Process -FilePath $Litellm -ArgumentList "--config", "config\litellm-proxy.example.yaml", `
    "--port", "4000", "--num_workers", "1" `
    -RedirectStandardOutput "$Root\litellm.out.log" -RedirectStandardError "$Root\litellm.err.log" `
    -WindowStyle Hidden -PassThru
Write-Host "    litellm PID: $($Lit.Id)（日志：litellm.out.log / litellm.err.log）"

# 4) 就绪探测（最多 40s）
$okServe = $false; $okLit = $false
for ($i = 0; $i -lt 40; $i++) {
    if (-not $okServe) { try { if ((Invoke-RestMethod "http://127.0.0.1:10041/health" -TimeoutSec 2).status -eq "ok") { $okServe = $true; Write-Host "    serve 就绪 ✓" } } catch {} }
    if (-not $okLit) { try { Invoke-RestMethod "http://127.0.0.1:4000/v1/models" -TimeoutSec 2 -Headers @{Authorization = "Bearer sk-router-bridge"} | Out-Null; $okLit = $true; Write-Host "    litellm 就绪 ✓" } catch {} }
    if ($okServe -and $okLit) { break }
    Start-Sleep -Seconds 1
}
if (-not ($okServe -and $okLit)) {
    Write-Host "警告：服务未完全就绪。查看 serve.err.log / litellm.err.log 定位问题。" -ForegroundColor Yellow
    exit 1
}
Write-Host ""
Write-Host "网关已就绪：serve=10041（cn-llm-router）/ litellm=4000（Claude Code 上游）"
Write-Host "Claude Code 配置：ANTHROPIC_BASE_URL=http://127.0.0.1:4000  ANTHROPIC_AUTH_TOKEN=sk-router-bridge  ANTHROPIC_MODEL=router-auto"
Write-Host "查看最近路由：curl http://127.0.0.1:10041/v1/routes"
