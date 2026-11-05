param([ValidateSet('Prepare','Start','Stop','Restart','Status','Build')][string]$Action = 'Start')
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$ProjectRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
Import-Module (Join-Path $PSScriptRoot 'Platform.psm1') -Force
try {
    if ($Action -eq 'Build') { & (Join-Path $PSScriptRoot 'Build.ps1'); exit $LASTEXITCODE }
    $context = New-YtxContext -ProjectRoot $ProjectRoot
    switch ($Action) {
        'Prepare' { Test-YtxDependencies -Context $context }
        'Start' { Start-YtxInfrastructure -Context $context; Start-YtxApplication -Context $context }
        'Stop' { Stop-YtxApplication -Context $context; Stop-YtxInfrastructure -Context $context }
        'Restart' { Stop-YtxApplication -Context $context; Start-YtxInfrastructure -Context $context; Start-YtxApplication -Context $context }
        'Status' { Get-YtxStatus -Context $context }
    }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
