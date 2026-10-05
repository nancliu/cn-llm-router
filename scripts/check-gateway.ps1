# cn-llm-router 网关自愈检查（供 Windows 计划任务周期调用）
# 逻辑：serve(10041) 与 litellm(4000) 任一挂掉 → 调用 start-gateway.ps1 拉起；
#       120 秒内已有恢复动作则跳过（防并发重复重启）。
$Root = Split-Path -Parent $PSScriptRoot
$Lock = Join-Path $Root "_gateway.lock"

if (Test-Path $Lock) {
    $age = (Get-Date) - (Get-Item $Lock).LastWriteTime
    if ($age -lt [TimeSpan]::FromSeconds(120)) { exit 0 }
}

$serveOk = $false; $litOk = $false
try { if ((Invoke-RestMethod "http://127.0.0.1:10041/health" -TimeoutSec 3).status -eq "ok") { $serveOk = $true } } catch {}
try { Invoke-RestMethod "http://127.0.0.1:4000/v1/models" -TimeoutSec 3 -Headers @{Authorization = "Bearer sk-router-bridge"} | Out-Null; $litOk = $true } catch {}

if ($serveOk -and $litOk) { exit 0 }

# 至少一个挂了 → 恢复
Set-Content -Path $Lock -Value "recovering"
try {
    powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "start-gateway.ps1") | Out-Null
} catch {}
Remove-Item $Lock -ErrorAction SilentlyContinue
