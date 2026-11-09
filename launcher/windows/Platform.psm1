Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function New-YtxContext {
    param([string]$ProjectRoot)
    $rootPath = [IO.Path]::GetFullPath($ProjectRoot)
    if ($rootPath.Length -gt 120) {
        $longPaths = Get-ItemPropertyValue -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -ErrorAction SilentlyContinue
        if ($longPaths -ne 1) { throw 'This checkout path requires Windows long paths. Move it to a shorter path or enable the Windows long-path policy, then retry.' }
    }
    return @{ Root = $rootPath; Data = (Join-Path $rootPath 'yutaixian_data');
        Runtime = (Join-Path $rootPath 'yutaixian_data\local_dev');
        Packages = (Join-Path $rootPath 'yutaixian_data\windows\packages') }
}

function Read-YtxManifest {
    param($Context)
    $file = Join-Path $Context.Root 'launcher\windows\dependencies.json'
    $manifest = [IO.File]::ReadAllText($file, [Text.Encoding]::UTF8) | ConvertFrom-Json
    if ($manifest.schema_version -ne 2 -or $manifest.platform -ne 'windows-x86_64') {
        throw 'Unsupported Windows dependency manifest.'
    }
    foreach ($entry in $manifest.dependencies) {
        if ($entry.sha256 -notmatch '^[a-fA-F0-9]{64}$' -or $entry.source -notmatch '^https://') {
            throw ('Missing HTTPS source or SHA256 for ' + $entry.name)
        }
        if ($entry.name -notmatch '^[a-z-]+$' -or $entry.version -notmatch '^[0-9.]+$' -or
            $entry.executable -match '(^[\\/]|:|(^|[\\/])\.\.([\\/]|$))') {
            throw 'Invalid dependency path.'
        }
    }
    return $manifest
}



function Invoke-YtxProcess {
    param([string]$Executable, [string[]]$Arguments, [switch]$PassExitCode)
    # Explicitly wait for the native child; do not rely on shell LASTEXITCODE.
    $quoted = foreach ($argument in $Arguments) {
        $escaped = [regex]::Replace($argument, '(\\*)"', '$1$1\"')
        $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
        '"' + $escaped + '"'
    }
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $Executable
    $info.Arguments = $quoted -join ' '
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = New-Object Text.UTF8Encoding($false)
    $info.StandardErrorEncoding = New-Object Text.UTF8Encoding($false)
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    try {
        [void]$process.Start()
        $errors = $process.StandardError.ReadToEndAsync()
        while (!$process.StandardOutput.EndOfStream) { Write-Host $process.StandardOutput.ReadLine() }
        $process.WaitForExit()
        $errorText = $errors.GetAwaiter().GetResult()
        if ($errorText) { Write-Host $errorText }
        if ($PassExitCode) { return $process.ExitCode }
        if ($process.ExitCode -ne 0) { throw ('Process failed (exit ' + $process.ExitCode + '): ' + $Executable) }
    } finally { $process.Dispose() }
}

function Install-YtxArchive {
    param($Context, $Entry)
    $destination = Join-Path $Context.Packages ($Entry.name + '-' + $Entry.version)
    $executable = Join-Path $destination $Entry.executable
    $marker = Join-Path $destination '.complete'
    if ((Test-Path -LiteralPath $executable) -and (Test-Path -LiteralPath $marker) -and
        ([IO.File]::ReadAllText($marker).Trim() -eq $Entry.sha256)) { return $executable }
    $cache = Join-Path $Context.Data 'windows\downloads'
    [IO.Directory]::CreateDirectory($cache) | Out-Null
    [IO.Directory]::CreateDirectory($Context.Packages) | Out-Null
    $archive = Join-Path $cache ($Entry.name + '-' + $Entry.version + '.' + $Entry.archive)
    if (!(Test-Path -LiteralPath $archive) -or
        (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $Entry.sha256) {
        Write-Host ('Downloading ' + $Entry.name + ' ' + $Entry.version + ' ...')
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $partial = $archive + '.partial'
        $client = New-Object Net.WebClient
        try { $client.DownloadFile($Entry.source, $partial) } finally { $client.Dispose() }
        if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash -ne $Entry.sha256) {
            throw ('SHA256 mismatch: ' + $Entry.name + '. The archive was not extracted or executed.')
        }
        Move-Item -LiteralPath $partial -Destination $archive -Force
    }
    $stage = $destination + '.extract-' + [Guid]::NewGuid().ToString('N')
    [IO.Directory]::CreateDirectory($stage) | Out-Null
    # Only hash-verified archives reach the extractor. Incomplete directories are
    # retained for diagnostics and never treated as a successful installation.
    if ($Entry.archive -eq 'zip') {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($archive, $stage)
    } elseif ($Entry.archive -eq 'tar.gz') {
        $tar = Join-Path $env:SystemRoot 'System32\tar.exe'
        if (!(Test-Path -LiteralPath $tar)) { throw 'Windows 10 1809+ tar.exe is required.' }
        Invoke-YtxProcess -Executable $tar -Arguments @('-xf', $archive, '-C', $stage)
    } else { throw 'Unsupported archive type.' }
    # Some Redis releases wrap the payload in one directory.
    if (!(Test-Path -LiteralPath (Join-Path $stage $Entry.executable))) {
        throw ('Archive layout differs from the reviewed manifest: ' + $Entry.name)
    }
    if (Test-Path -LiteralPath $destination) {
        $verifiedDestination = [IO.Path]::GetFullPath($destination)
        $verifiedBase = [IO.Path]::GetFullPath($Context.Packages).TrimEnd('\') + '\'
        if (!$verifiedDestination.StartsWith($verifiedBase, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Refusing to move a path outside the package directory.'
        }
        Move-Item -LiteralPath $destination -Destination ($destination + '.incomplete-' + [Guid]::NewGuid().ToString('N'))
    }
    Move-Item -LiteralPath $stage -Destination $destination
    [IO.File]::WriteAllText($marker, $Entry.sha256, (New-Object Text.UTF8Encoding($false)))
    return $executable
}

function Invoke-YtxAction {
    param($Context, [string]$Action, [switch]$NoBrowser)
    if (![Environment]::Is64BitOperatingSystem -or ![Environment]::Is64BitProcess) {
        throw 'Run the launcher with 64-bit Windows PowerShell.'
    }
    if ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() -ne 'X64') { throw 'This runtime requires Windows x64.' }
    $manifest = Read-YtxManifest -Context $Context
    $pythonEntry = @($manifest.dependencies | Where-Object { $_.name -eq 'python' })[0]
    $python = Join-Path $Context.Packages ('python-' + $pythonEntry.version + '\' + $pythonEntry.executable)
    if ($Action -in @('Stop','Status','StopApplication','StopInfrastructure') -and !(Test-Path -LiteralPath $python)) {
        $record = Join-Path $Context.Runtime 'state.json'
        $infraRecord = Join-Path $Context.Runtime 'infrastructure.json'
        if ((Test-Path -LiteralPath $record) -or (Test-Path -LiteralPath $infraRecord)) {
            throw 'The runtime Python is missing. Restore it with Prepare before managing existing processes.'
        }
        Write-Host 'Yutaixian has not been started; no process was changed.'
        return
    }
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $digest = ([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($Context.Root.ToLowerInvariant())))).Replace('-','').Substring(0,24).ToLowerInvariant() }
    finally { $hash.Dispose() }
    $mutex = New-Object Threading.Mutex($false, ('Global\Yutaixian.' + $digest + '.bootstrap.'))
    $locked = $false
    try {
        try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
        if (!$locked) { throw 'Another launcher operation is in progress.' }
        if ($Action -notin @('Stop','Status','StopApplication','StopInfrastructure')) {
            $python = Install-YtxArchive -Context $Context -Entry $pythonEntry
        }
        $env:PYTHONUTF8 = '1'
        $env:PYTHONUNBUFFERED = '1'
        $arguments = @((Join-Path $Context.Root 'scripts\windows_runtime.py'), $Action, '--root', $Context.Root)
        if ($NoBrowser) { $arguments += '--no-browser' }
        $exitCode = Invoke-YtxProcess -Executable $python -Arguments $arguments -PassExitCode
        if ($exitCode -eq 86) {
            # Keep browser file installation separate from the Python driver
            # process, then resume the same action and run browser launch probes.
            $toolsPath = Join-Path $Context.Runtime 'windows-tools.json'
            $runtimeTools = [IO.File]::ReadAllText($toolsPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
            Invoke-YtxProcess -Executable $runtimeTools.node -Arguments @((Join-Path $Context.Root 'scripts\windows_browsers.cjs'), $Context.Root)
            Invoke-YtxProcess -Executable $python -Arguments $arguments
        } elseif ($exitCode -ne 0) { throw ('Windows runtime failed (exit ' + $exitCode + ').') }
    } finally {
        if ($locked) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

function Test-YtxDependencies { param($Context) Invoke-YtxAction $Context 'Prepare' }
function Start-YtxInfrastructure { param($Context) Invoke-YtxAction $Context 'StartInfrastructure' }
function Stop-YtxInfrastructure { param($Context) Invoke-YtxAction $Context 'StopInfrastructure' }
function Start-YtxApplication { param($Context) Invoke-YtxAction $Context 'StartApplication' }
function Stop-YtxApplication { param($Context) Invoke-YtxAction $Context 'StopApplication' }
function Get-YtxStatus { param($Context) Invoke-YtxAction $Context 'Status' }
Export-ModuleMember -Function New-YtxContext, Invoke-YtxAction, Test-YtxDependencies, Start-YtxInfrastructure, Stop-YtxInfrastructure, Start-YtxApplication, Stop-YtxApplication, Get-YtxStatus
