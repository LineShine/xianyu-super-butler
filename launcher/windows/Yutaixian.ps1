param(
    [ValidateSet('Prepare','Start','Stop','Restart','Status','Build')][string]$Action = 'Start',
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
Set-StrictMode -Version Latest
$ProjectRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
try {
    if ($Action -eq 'Build') { & (Join-Path $PSScriptRoot 'Build.ps1'); exit 0 }
    Import-Module (Join-Path $PSScriptRoot 'Platform.psm1') -Force
    $context = New-YtxContext -ProjectRoot $ProjectRoot
    Invoke-YtxAction -Context $context -Action $Action -NoBrowser:$NoBrowser
    exit 0
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
