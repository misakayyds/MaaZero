#Requires -Version 5.1
[CmdletBinding()]
param([string]$RepoRoot)

$ErrorActionPreference = 'Stop'
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path }
$buildScript = Join-Path $PSScriptRoot '..\build-project.ps1'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$checks = 0
$failures = New-Object 'System.Collections.Generic.List[string]'

function Assert-That { param([bool]$Condition, [string]$Message); $script:checks++; if (-not $Condition) { $script:failures.Add($Message) } }
function Write-Manifest { param([string]$Path, [string[]]$Files); [System.IO.File]::WriteAllText($Path, (([ordered]@{ schema = 1; files = @($Files) } | ConvertTo-Json -Depth 4) + "`n"), $utf8) }
function Invoke-Build {
    param([hashtable]$Parameters)
    try {
        $output = & $buildScript @Parameters *>&1 | Out-String
        return @{ ExitCode = $LASTEXITCODE; Output = $output }
    } catch {
        return @{ ExitCode = 1; Output = $_.Exception.Message }
    }
}
function New-TestRoot { $root = Join-Path ([System.IO.Path]::GetTempPath()) ('maa-project-allowlist-' + [guid]::NewGuid().ToString('N')); New-Item -ItemType Directory -Force -Path $root | Out-Null; return $root }
function New-TestSource {
    param([string]$Root)
    $src = Join-Path $Root 'src'; New-Item -ItemType Directory -Force -Path $src | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $src 'maa-run.sh'), "#!/bin/sh`n", $utf8)
    [System.IO.File]::WriteAllText((Join-Path $src 'maa-auto-update.sh'), "#!/bin/sh`n", $utf8)
    $webui = @('PORT = 8088', 'server = WEB_SERVER_CLASS(("0.0.0.0", PORT), Handler)', 'print("http://0.0.0.0:{PORT}")') -join "`n"
    [System.IO.File]::WriteAllText((Join-Path $src 'maa-webui.py'), $webui + "`n", $utf8)
    [System.IO.File]::WriteAllText((Join-Path $src 'maa-weekly-editor.js'), "// editor`n", $utf8)
    return $src
}

$root = New-TestRoot
try {
    $src = New-TestSource $root; $manifest = Join-Path $root 'project-files.json'
    Write-Manifest $manifest @('maa-run.sh', 'maa-auto-update.sh', 'maa-webui.py', 'maa-weekly-editor.js')
    $r = Invoke-Build @{ Source = $src; ProjectFiles = $manifest; Out = (Join-Path $root 'out-allowlist') }
    Assert-That ($r.ExitCode -eq 0) "custom allowlist build must succeed: $($r.Output)"
    Assert-That (Test-Path -LiteralPath (Join-Path $root 'out-allowlist\project\maa-weekly-editor.js')) 'listed file must be packaged'
    $r = Invoke-Build @{ Source = $src; ProjectFiles = $manifest; Out = (Join-Path $root 'out-allowlist') }
    Assert-That ($r.ExitCode -ne 0) "existing output directory must not be overwritten: $($r.Output)"

    [System.IO.File]::WriteAllText((Join-Path $src 'unexpected.py'), "print(1)`n", $utf8)
    $r = Invoke-Build @{ Source = $src; ProjectFiles = $manifest; Out = (Join-Path $root 'out-unknown') }
    Assert-That ($r.ExitCode -ne 0 -and $r.Output -match 'allowlist|unknown|unexpected.py') 'unknown legal file must block build'
    Remove-Item -LiteralPath (Join-Path $src 'unexpected.py') -Force

    Write-Manifest $manifest @('maa-run.sh', 'maa-auto-update.sh', 'maa-webui.py', 'maa-weekly-editor.js', 'missing.py')
    $r = Invoke-Build @{ Source = $src; ProjectFiles = $manifest; Out = (Join-Path $root 'out-missing') }
    Assert-That ($r.ExitCode -ne 0 -and $r.Output -match 'missing.py') "missing listed file must block build: $($r.Output)"
    Write-Manifest $manifest @('maa-run.sh', 'maa-run.sh', 'maa-auto-update.sh', 'maa-webui.py')
    $r = Invoke-Build @{ Source = $src; ProjectFiles = $manifest; Out = (Join-Path $root 'out-duplicate') }
    Assert-That ($r.ExitCode -ne 0 -and $r.Output -match 'maa-run.sh') "duplicate list entry must block build: $($r.Output)"
    Write-Manifest $manifest @('maa-run.sh', '..\evil.sh', 'maa-auto-update.sh', 'maa-webui.py')
    $r = Invoke-Build @{ Source = $src; ProjectFiles = $manifest; Out = (Join-Path $root 'out-traversal') }
    Assert-That ($r.ExitCode -ne 0 -and $r.Output -match 'traversal|unsafe|evil.sh') 'traversal list entry must block build'
    Write-Manifest $manifest @('maa-run.sh', 'tg.txt', 'maa-auto-update.sh', 'maa-webui.py')
    $r = Invoke-Build @{ Source = $src; ProjectFiles = $manifest; Out = (Join-Path $root 'out-credential') }
    Assert-That ($r.ExitCode -ne 0 -and $r.Output -match 'credential|tg.txt') 'credential list entry must block build'
    Write-Manifest $manifest @('maa-run.sh', 'start_webui.sh', 'maa-auto-update.sh', 'maa-webui.py')
    $r = Invoke-Build @{ Source = $src; ProjectFiles = $manifest; Out = (Join-Path $root 'out-device') }
    Assert-That ($r.ExitCode -ne 0 -and $r.Output -match 'device|start_webui') 'device-danger list entry must block build'

    $realCopy = Join-Path $root 'real-copy'; Copy-Item -LiteralPath (Join-Path $RepoRoot 'packaging\project\source') -Destination $realCopy -Recurse
    [System.IO.File]::WriteAllText((Join-Path $realCopy 'unexpected.py'), "print(1)`n", $utf8)
    $r = Invoke-Build @{ Source = $realCopy; Out = (Join-Path $root 'out-default') }
    Assert-That ($r.ExitCode -ne 0 -and $r.Output -match 'allowlist|unknown|unexpected.py') 'default list must be fixed outside Source'

    $sourceAliasCreated = $false
    $sourceAlias = Join-Path $root 'source-alias'
    try {
        New-Item -ItemType Junction -Path $sourceAlias -Target $src -ErrorAction Stop | Out-Null
        $sourceAliasCreated = $true
    } catch { }
    if ($sourceAliasCreated) {
        $r = Invoke-Build @{ Source = $sourceAlias; ProjectFiles = $manifest; Out = (Join-Path $root 'out-source-alias') }
        Assert-That ($r.ExitCode -ne 0) "source root junction must be rejected: $($r.Output)"
    } else {
        Write-Host 'PACKAGING_PROJECT_ALLOWLIST_SOURCE_ROOT_TEST_SKIPPED'
    }

    $linkCreated = $false
    try { New-Item -ItemType SymbolicLink -Path (Join-Path $src 'linked.sh') -Target (Join-Path $src 'maa-run.sh') -ErrorAction Stop | Out-Null; $linkCreated = $true } catch { }
    if ($linkCreated) {
        Write-Manifest $manifest @('maa-run.sh', 'maa-auto-update.sh', 'maa-webui.py', 'maa-weekly-editor.js', 'linked.sh')
        $r = Invoke-Build @{ Source = $src; ProjectFiles = $manifest; Out = (Join-Path $root 'out-link') }
        Assert-That ($r.ExitCode -ne 0 -and $r.Output -match 'symlink|reparse|link') 'source link must be rejected'
    } else {
        $junctionCreated = $false
        $targetDir = Join-Path $root 'junction-target'
        New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
        try {
            New-Item -ItemType Junction -Path (Join-Path $src 'linked-dir') -Target $targetDir -ErrorAction Stop | Out-Null
            $junctionCreated = $true
        } catch { }
        if ($junctionCreated) {
            $r = Invoke-Build @{ Source = $src; ProjectFiles = $manifest; Out = (Join-Path $root 'out-junction') }
            Assert-That ($r.ExitCode -ne 0) "source junction must be rejected: $($r.Output)"
        } else {
            Write-Host 'PACKAGING_PROJECT_ALLOWLIST_LINK_TEST_SKIPPED'
        }
    }
} finally { if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force } }

if ($failures.Count -gt 0) { Write-Host "PACKAGING_PROJECT_ALLOWLIST_FAIL checks=$checks failures=$($failures.Count)"; foreach ($failure in $failures) { Write-Host " - $failure" }; exit 1 }
Write-Host "PACKAGING_PROJECT_ALLOWLIST_OK checks=$checks"
exit 0
