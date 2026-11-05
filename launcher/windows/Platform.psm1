Set-StrictMode -Version Latest

function New-YtxContext {
    param([string]$ProjectRoot)
    return @{ Root = $ProjectRoot; Data = (Join-Path $ProjectRoot 'yutaixian_data');
        SecretRoot = (Join-Path $env:LOCALAPPDATA 'Yutaixian\secrets') }
}

function Assert-YtxWindowsAdapter {
    throw 'Windows 框架已就绪，原生运行适配尚待 Windows 实机补齐。请按 launcher/windows/AI-HANDOFF.md 完成后使用；未启动、停止或覆盖任何数据库。'
}

# Keep infrastructure and applications independently owned. Each implementation
# must verify executable path + creation time + launch nonce before signalling.
function Test-YtxDependencies { param($Context) Assert-YtxWindowsAdapter }
function Start-YtxInfrastructure { param($Context) Assert-YtxWindowsAdapter }
function Stop-YtxInfrastructure { param($Context) Assert-YtxWindowsAdapter }
function Start-YtxApplication { param($Context) Assert-YtxWindowsAdapter }
function Stop-YtxApplication { param($Context) Assert-YtxWindowsAdapter }
function Get-YtxStatus {
    param($Context)
    Write-Host 'Windows 原生适配状态：框架；未完成实机验收。'
    Write-Host ('数据目录：' + $Context.Data)
}
Export-ModuleMember -Function New-YtxContext, Test-YtxDependencies, Start-YtxInfrastructure, Stop-YtxInfrastructure, Start-YtxApplication, Stop-YtxApplication, Get-YtxStatus
