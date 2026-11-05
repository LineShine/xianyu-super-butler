$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$Payload = Join-Path $Root 'build_output\windows\payload'
$Marker = Join-Path $Payload 'windows-validation.json'
if (!(Test-Path $Marker)) {
    throw '尚未完成 Windows 组件补齐与实机验收，不能生成或宣称可用安装包。参见 AI-HANDOFF.md。'
}
$Validation = Get-Content $Marker -Raw | ConvertFrom-Json
if ($Validation.status -ne 'passed') { throw 'Windows 验收尚未通过。' }
$Compiler = Get-Command ISCC.exe -ErrorAction Stop
& $Compiler.Source "/DPayloadDir=$Payload" (Join-Path $PSScriptRoot 'installer.iss')
if ($LASTEXITCODE -ne 0) { throw '安装器构建失败。' }
