#Requires -Version 5.1
<#
.SYNOPSIS
    Behaviour tests for packaging/build-module.ps1.
.DESCRIPTION
    Every case builds a real ZIP from synthetic fixtures in a temporary
    directory and inspects the result: ELF validation, explicit preview
    naming, LF-normalised entries, unix modes, local tutorial, metadata and
    the refusal paths (missing/invalid binary, non-preview version, unsafe
    release config, missing webui assets, missing user guide).
    Nothing here installs a module or touches a device.
#>
[CmdletBinding()]
param(
    [string]$BuildScript = "",
    [string]$ModuleRoot = "",
    [string]$UserGuidePath = ""
)

$ErrorActionPreference = "Stop"

if ($BuildScript.Length -eq 0) { $BuildScript = Join-Path $PSScriptRoot "build-module.ps1" }
if ($ModuleRoot.Length -eq 0) { $ModuleRoot = Join-Path (Join-Path $PSScriptRoot "..") "module" }
if ($UserGuidePath.Length -eq 0) { $UserGuidePath = Join-Path (Join-Path (Join-Path $PSScriptRoot "..") "docs") "user-guide.md" }

# Resolve-Path can yield an empty array instead of $null, so collapse to a scalar.
function Resolve-ExistingPath {
    param([string]$Path, [string]$Name)
    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $resolved) { throw "required $Name not found: $Path" }
    return $resolved.Path
}

try {
    $BuildScript = Resolve-ExistingPath $BuildScript "build script"
    $ModuleRoot = Resolve-ExistingPath $ModuleRoot "module source tree"
    $UserGuidePath = Resolve-ExistingPath $UserGuidePath "offline user guide"
} catch {
    Write-Host "MODULE_PACKAGING_FAIL: $($_.Exception.Message)"
    exit 1
}

$script:passed = 0
$script:failed = New-Object "System.Collections.Generic.List[string]"

function Check {
    param([bool]$Condition, [string]$Name)
    if ($Condition) { $script:passed++ } else { $script:failed.Add($Name) }
}

function CheckEqual {
    param($Actual, $Expected, [string]$Name)
    if ($Actual -eq $Expected) { $script:passed++ } else { $script:failed.Add("$Name (got=[$Actual] want=[$Expected])") }
}

function Expect-Refusal {
    param([scriptblock]$Action, [string]$Name, [string]$MessageMustContain)
    try {
        & $Action | Out-Null
    } catch {
        if ($_.Exception.Message -like ("*" + $MessageMustContain + "*")) { $script:passed++ }
        else { $script:failed.Add("$Name (refused but message lacks [$MessageMustContain]: $($_.Exception.Message))") }
        return
    }
    $script:failed.Add("$Name (expected refusal, build succeeded)")
}

function New-ElfFixture {
    param(
        [string]$Path,
        [byte]$Class = 2,
        [byte]$Data = 1,
        [uint16]$Machine = 0xB7,
        [uint16]$Type = 2,
        [switch]$Interp,
        [int]$Payload = 64
    )
    $shnum = 0
    if ($Interp) { $shnum = 1 }
    $headerSize = 64 + ((1 + $shnum) * 56) + $Payload
    $bytes = New-Object "byte[]" $headerSize
    $bytes[0] = 0x7F; $bytes[1] = 0x45; $bytes[2] = 0x4C; $bytes[3] = 0x46
    $bytes[4] = $Class; $bytes[5] = $Data; $bytes[6] = 1
    [BitConverter]::GetBytes($Type).CopyTo($bytes, 16)
    [BitConverter]::GetBytes($Machine).CopyTo($bytes, 18)
    [BitConverter]::GetBytes([uint32]1).CopyTo($bytes, 20)
    [BitConverter]::GetBytes([uint64]64).CopyTo($bytes, 32)
    [BitConverter]::GetBytes([uint16]64).CopyTo($bytes, 52)
    [BitConverter]::GetBytes([uint16]56).CopyTo($bytes, 54)
    [BitConverter]::GetBytes([uint16](1 + $shnum)).CopyTo($bytes, 56)
    # program header 0: PT_LOAD
    [BitConverter]::GetBytes([uint32]1).CopyTo($bytes, 64)
    [BitConverter]::GetBytes([uint32]5).CopyTo($bytes, 68)
    [BitConverter]::GetBytes([uint64]$headerSize).CopyTo($bytes, 72)
    [BitConverter]::GetBytes([uint64]$headerSize).CopyTo($bytes, 96)
    if ($Interp) {
        # program header 1: PT_INTERP (3)
        [BitConverter]::GetBytes([uint32]3).CopyTo($bytes, 120)
    }
    for ($i = $headerSize - $Payload; $i -lt $headerSize; $i++) { $bytes[$i] = 0x90 }
    [System.IO.File]::WriteAllBytes($Path, $bytes)
    return $Path
}

function New-WebuiFixture {
    param([string]$Directory)
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $Directory "module-settings.js"), "export const settings = {};`n")
    [System.IO.File]::WriteAllText((Join-Path $Directory "module-settings.css"), ".settings { color: inherit; }`n")
    return $Directory
}

function Read-ZipEntries {
    param([string]$Path)
    $entries = @{}
    $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
    try {
        foreach ($entry in $archive.Entries) {
            $stream = $entry.Open()
            $memory = New-Object "System.IO.MemoryStream"
            try { $stream.CopyTo($memory) } finally { $stream.Dispose() }
            $entries[$entry.FullName] = [pscustomobject]@{
                Bytes = $memory.ToArray()
                Mode = ($entry.ExternalAttributes -shr 16) -band 0xFFF
            }
            $memory.Dispose()
        }
    } finally { $archive.Dispose() }
    return $entries
}

function Get-Sha256 {
    param([byte[]]$Bytes)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return (($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString("x2") }) -join "") }
    finally { $sha.Dispose() }
}

function Get-Text {
    param([byte[]]$Bytes)
    return [System.Text.Encoding]::UTF8.GetString($Bytes)
}

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("maa-module-test-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tempRoot | Out-Null

$version = "0.1.0-preview.7"
$goodBinary = New-ElfFixture -Path (Join-Path $tempRoot "maa-managerd")
$webui = New-WebuiFixture -Directory (Join-Path $tempRoot "webui")
$publicKey = [Convert]::ToBase64String((New-Object "byte[]" 32))
$releaseConfig = Join-Path $tempRoot "release.json"
[System.IO.File]::WriteAllText($releaseConfig, "{`n  `"manifest_url`": `"https://example.invalid/stable.json`",`n  `"public_key`": `"$publicKey`",`n  `"channel`": `"stable`"`n}`n")
$httpConfig = Join-Path $tempRoot "http-release.json"
[System.IO.File]::WriteAllText($httpConfig, "{`n  `"manifest_url`": `"http://example.invalid/stable.json`",`n  `"public_key`": `"$publicKey`",`n  `"channel`": `"stable`"`n}`n")
$missingKeyConfig = Join-Path $tempRoot "missing-key-release.json"
[System.IO.File]::WriteAllText($missingKeyConfig, "{`n  `"manifest_url`": `"https://example.invalid/stable.json`",`n  `"channel`": `"stable`"`n}`n")
$badChannelConfig = Join-Path $tempRoot "bad-channel-release.json"
[System.IO.File]::WriteAllText($badChannelConfig, "{`n  `"manifest_url`": `"https://example.invalid/stable.json`",`n  `"public_key`": `"$publicKey`",`n  `"channel`": `"nightly`"`n}`n")
$shortKeyConfig = Join-Path $tempRoot "short-key-release.json"
[System.IO.File]::WriteAllText($shortKeyConfig, "{`n  `"manifest_url`": `"https://example.invalid/stable.json`",`n  `"public_key`": `"AAAA`",`n  `"channel`": `"stable`"`n}`n")
$badBase64Config = Join-Path $tempRoot "bad-base64-release.json"
[System.IO.File]::WriteAllText($badBase64Config, "{`n  `"manifest_url`": `"https://example.invalid/stable.json`",`n  `"public_key`": `"not-base64!!`",`n  `"channel`": `"stable`"`n}`n")
$extraKeyConfig = Join-Path $tempRoot "extra-key-release.json"
[System.IO.File]::WriteAllText($extraKeyConfig, "{`n  `"manifest_url`": `"https://example.invalid/stable.json`",`n  `"public_key`": `"$publicKey`",`n  `"channel`": `"stable`",`n  `"listen`": `"0.0.0.0:8088`"`n}`n")
$badElf = Join-Path $tempRoot "not-an-elf"
[System.IO.File]::WriteAllText($badElf, "this is not an elf`n")
$elf32 = New-ElfFixture -Path (Join-Path $tempRoot "elf32") -Class 1 -Machine 0x28
$elfX64 = New-ElfFixture -Path (Join-Path $tempRoot "elf-x86_64") -Machine 0x3E
$elfDynamic = New-ElfFixture -Path (Join-Path $tempRoot "elf-dynamic") -Interp
$exeBinary = New-ElfFixture -Path (Join-Path $tempRoot "maa-managerd.exe")
$badConfig = Join-Path $tempRoot "bad-release.json"
[System.IO.File]::WriteAllText($badConfig, "{ not json " )
$secretConfig = Join-Path $tempRoot "secret-release.json"
[System.IO.File]::WriteAllText($secretConfig, "{`n  `"download_token`": `"abc`"`n}`n")
$largeConfig = Join-Path $tempRoot "large-release.json"
[System.IO.File]::WriteAllText($largeConfig, "{ `"pad`": `"" + ("x" * 70000) + "`" }")
$emptyWebui = Join-Path $tempRoot "webui-empty"
New-Item -ItemType Directory -Path $emptyWebui -Force | Out-Null

function Invoke-Build {
    param([hashtable]$Parameters)
    return (& $BuildScript @Parameters | Select-Object -Last 1)
}

try {
    # 1. The repository default webui directory may not have the parallel
    #    agent asset yet: either it builds, or it refuses and names both files.
    $defaultOut = Join-Path $tempRoot "out-default"
    $defaultWebui = Join-Path (Join-Path $PSScriptRoot "..") "webui"
    $hasJs = Test-Path -LiteralPath (Join-Path $defaultWebui "module-settings.js") -PathType Leaf
    $hasCss = Test-Path -LiteralPath (Join-Path $defaultWebui "module-settings.css") -PathType Leaf
    $defaultParams = @{
        ManagerBinary = $goodBinary; Version = $version; VersionCode = 3
        OutputDirectory = $defaultOut; WebuiDirectory = $defaultWebui; UserGuidePath = $UserGuidePath
    }
    if ($hasJs -and $hasCss) {
        $output = Invoke-Build $defaultParams
        Check ($output -like "MODULE_ZIP_OK*") "default webui assets should build"
    } else {
        Expect-Refusal { Invoke-Build $defaultParams } "missing webui assets are refused" "module-settings.js"
        Expect-Refusal { Invoke-Build $defaultParams } "missing webui assets mention css" "module-settings.css"
    }

    # 2. Full preview build with every input supplied.
    $out = Join-Path $tempRoot "out-full"
    $output = Invoke-Build @{
        ManagerBinary = $goodBinary; Version = $version; VersionCode = 3
        OutputDirectory = $out; WebuiDirectory = $webui; UserGuidePath = $UserGuidePath
        ReleaseConfig = $releaseConfig
    }
    Check ($output -like "MODULE_ZIP_OK*") "full preview build reports MODULE_ZIP_OK"
    $zipPath = Join-Path $out ("MAA-Phone-Manager-preview-" + $version + ".zip")
    Check (Test-Path -LiteralPath $zipPath -PathType Leaf) "preview zip name contains preview and version"
    Check ((Split-Path -Leaf $zipPath) -like "MAA-Phone-Manager-preview-*") "artifact filename is explicitly marked as preview"

    $entries = Read-ZipEntries $zipPath
    $expected = @(
        "module.prop", "customize.sh", "service.sh", "action.sh", "uninstall.sh",
        "webroot/index.html", "webroot/assets/module-settings.js", "webroot/assets/module-settings.css",
        "webroot/assets/user-guide.md", "bin/maa-managerd", "release.json",
        "README-preview.md", "module-metadata.json"
    )
    $missing = @($expected | Where-Object { -not $entries.ContainsKey($_) })
    Check ($missing.Count -eq 0) ("zip is missing expected entries: " + ($missing -join ", "))
    $unexpected = @($entries.Keys | Where-Object { $expected -notcontains $_ })
    Check ($unexpected.Count -eq 0) ("zip contains unexpected entries: " + ($unexpected -join ", "))
    Check ($entries.Count -eq $expected.Count) "zip entry count matches the allowlist"

    CheckEqual (Get-Sha256 $entries["bin/maa-managerd"].Bytes) (Get-Sha256 ([System.IO.File]::ReadAllBytes($goodBinary))) "binary bytes are shipped unchanged"
    CheckEqual (Get-Sha256 $entries["webroot/assets/module-settings.js"].Bytes) (Get-Sha256 ([System.IO.File]::ReadAllBytes((Join-Path $webui "module-settings.js")))) "webui js asset is shipped unchanged"
    CheckEqual (Get-Sha256 $entries["webroot/assets/user-guide.md"].Bytes) (Get-Sha256 ([System.IO.File]::ReadAllBytes($UserGuidePath))) "user guide is copied as the offline tutorial"

    $propText = Get-Text $entries["module.prop"].Bytes
    Check ($propText -like "*version=$version*") "module.prop carries the requested preview version"
    Check ($propText -like "*versionCode=3*") "module.prop carries the requested versionCode"
    Check ($propText -notmatch "0.0.0-preview") "module.prop placeholder version is replaced"

    $commandCard = Get-Text $entries["service.sh"].Bytes
    Check ($commandCard -like "*127.0.0.1:8088*") "packaged service.sh keeps the loopback listen address"

    $tutorial = Get-Text $entries["README-preview.md"].Bytes
    Check ($tutorial -match "(?i)preview") "local tutorial marks the package as preview"
    Check ($tutorial -like "*/data/local/maa-platform*") "local tutorial names the data directory"

    $textEntries = @($entries.Keys | Where-Object { $_ -match "\.(sh|prop|md|json|html|js|css)$" })
    $crEntries = @($textEntries | Where-Object { $entries[$_].Bytes -contains 13 })
    Check ($crEntries.Count -eq 0) ("text entries must be LF only: " + ($crEntries -join ", "))
    $scriptEntries = @($entries.Keys | Where-Object { $_ -match "\.sh$" })
    $badModes = @($scriptEntries | Where-Object { $entries[$_].Mode -ne 0x1ED })
    Check ($badModes.Count -eq 0) ("scripts must carry unix mode 0755: " + ($badModes -join ", "))
    $dataModes = @($entries.Keys | Where-Object { $_ -match "\.(prop|md|json|html|js|css)$" } | Where-Object { $entries[$_].Mode -ne 0x1A4 })
    Check ($dataModes.Count -eq 0) ("data files must carry unix mode 0644: " + ($dataModes -join ", "))

    $leaked = @($entries.Keys | Where-Object { $_ -match "codex|\.git|tg\.txt|manager/|tests/|superpowers" })
    Check ($leaked.Count -eq 0) ("zip must not contain repository or user files: " + ($leaked -join ", "))

    $metadata = (Get-Text $entries["module-metadata.json"].Bytes) | ConvertFrom-Json
    CheckEqual $metadata.preview $true "metadata marks the preview build"
    CheckEqual $metadata.version $version "metadata carries the version"
    CheckEqual $metadata.webui_assets "included" "metadata records included webui assets"
    CheckEqual $metadata.release_config "included" "metadata records the public release config"
    CheckEqual $metadata.manager.sha256 (Get-Sha256 $entries["bin/maa-managerd"].Bytes) "metadata records the manager hash"
    CheckEqual $metadata.manager.architecture "arm64-v8a" "metadata records the target architecture"
    CheckEqual $metadata.data_root "/data/local/maa-platform" "metadata records the data root"
    $inventoryPaths = @($metadata.files | ForEach-Object { $_.path })
    $notInventoried = @($entries.Keys | Where-Object { $_ -ne "module-metadata.json" -and $inventoryPaths -notcontains $_ })
    Check ($notInventoried.Count -eq 0) ("metadata inventory misses: " + ($notInventoried -join ", "))
    $metaCrBytes = $entries["module-metadata.json"].Bytes
    Check (-not ($metaCrBytes -contains 13)) "module-metadata.json is LF only"

    # 3. Local preview without the parallel agent assets.
    $previewOut = Join-Path $tempRoot "out-preview"
    $output = Invoke-Build @{
        ManagerBinary = $goodBinary; Version = $version; OutputDirectory = $previewOut
        WebuiDirectory = $emptyWebui; UserGuidePath = $UserGuidePath; AllowMissingWebuiAssets = $true
    }
    Check ($output -like "MODULE_ZIP_OK*") "preview build without webui assets reports MODULE_ZIP_OK"
    $previewZip = Join-Path $previewOut ("MAA-Phone-Manager-preview-" + $version + ".zip")
    $previewEntries = Read-ZipEntries $previewZip
    Check (-not $previewEntries.ContainsKey("webroot/assets/module-settings.js")) "missing webui asset is not faked"
    Check ($previewEntries.ContainsKey("webroot/assets/user-guide.md")) "offline user guide is still shipped"
    $previewMeta = (Get-Text $previewEntries["module-metadata.json"].Bytes) | ConvertFrom-Json
    CheckEqual $previewMeta.webui_assets "missing" "metadata records missing webui assets"
    CheckEqual $previewMeta.release_config "absent" "metadata records an absent release config"

    # 4. Deterministic output for identical inputs.
    $repeatOut = Join-Path $tempRoot "out-repeat"
    $second = Invoke-Build @{
        ManagerBinary = $goodBinary; Version = $version; VersionCode = 3
        OutputDirectory = $repeatOut; WebuiDirectory = $webui; UserGuidePath = $UserGuidePath;
        ReleaseConfig = $releaseConfig
    }
    $repeatZip = Join-Path $repeatOut ("MAA-Phone-Manager-preview-" + $version + ".zip")
    CheckEqual (Get-Sha256 ([System.IO.File]::ReadAllBytes($repeatZip))) (Get-Sha256 ([System.IO.File]::ReadAllBytes($zipPath))) "two builds of the same inputs produce identical bytes"

    # 5. Refusals.
    Expect-Refusal { Invoke-Build @{ ManagerBinary = (Join-Path $tempRoot "missing-binary"); Version = $version; OutputDirectory = (Join-Path $tempRoot "out-r1"); WebuiDirectory = $webui; UserGuidePath = $UserGuidePath } } "missing manager binary is refused" "maa-managerd"
    Expect-Refusal { Invoke-Build @{ ManagerBinary = $badElf; Version = $version; OutputDirectory = (Join-Path $tempRoot "out-r2"); WebuiDirectory = $webui; UserGuidePath = $UserGuidePath } } "non-ELF binary is refused" "ELF"
    Expect-Refusal { Invoke-Build @{ ManagerBinary = $elf32; Version = $version; OutputDirectory = (Join-Path $tempRoot "out-r3"); WebuiDirectory = $webui; UserGuidePath = $UserGuidePath } } "32-bit binary is refused" "64-bit"
    Expect-Refusal { Invoke-Build @{ ManagerBinary = $elfX64; Version = $version; OutputDirectory = (Join-Path $tempRoot "out-r4"); WebuiDirectory = $webui; UserGuidePath = $UserGuidePath } } "x86_64 binary is refused" "arm64"
    Expect-Refusal { Invoke-Build @{ ManagerBinary = $elfDynamic; Version = $version; OutputDirectory = (Join-Path $tempRoot "out-r5"); WebuiDirectory = $webui; UserGuidePath = $UserGuidePath } } "dynamically linked binary is refused" "PT_INTERP"
    Expect-Refusal { Invoke-Build @{ ManagerBinary = $exeBinary; Version = $version; OutputDirectory = (Join-Path $tempRoot "out-r6"); WebuiDirectory = $webui; UserGuidePath = $UserGuidePath } } "windows executable name is refused" ".exe"
    Expect-Refusal { Invoke-Build @{ ManagerBinary = $goodBinary; Version = "1.0.0"; OutputDirectory = (Join-Path $tempRoot "out-r7"); WebuiDirectory = $webui; UserGuidePath = $UserGuidePath } } "release-looking version is refused" "preview"
    Expect-Refusal { Invoke-Build @{ ManagerBinary = $goodBinary; Version = "abc"; OutputDirectory = (Join-Path $tempRoot "out-r8"); WebuiDirectory = $webui; UserGuidePath = $UserGuidePath } } "malformed version is refused" "version"
    Expect-Refusal { Invoke-Build @{ ManagerBinary = $goodBinary; Version = $version; OutputDirectory = (Join-Path $tempRoot "out-r9"); WebuiDirectory = $webui; UserGuidePath = $UserGuidePath; ReleaseConfig = $badConfig } } "non-JSON release config is refused" "JSON"
    Expect-Refusal { Invoke-Build @{ ManagerBinary = $goodBinary; Version = $version; OutputDirectory = (Join-Path $tempRoot "out-r10"); WebuiDirectory = $webui; UserGuidePath = $UserGuidePath; ReleaseConfig = $secretConfig } } "secret-looking release config is refused" "token"
    Expect-Refusal { Invoke-Build @{ ManagerBinary = $goodBinary; Version = $version; OutputDirectory = (Join-Path $tempRoot "out-r11"); WebuiDirectory = $webui; UserGuidePath = $UserGuidePath; ReleaseConfig = $largeConfig } } "oversized release config is refused" "size"
    Expect-Refusal { Invoke-Build @{ ManagerBinary = $goodBinary; Version = $version; OutputDirectory = (Join-Path $tempRoot "out-c1"); WebuiDirectory = $webui; UserGuidePath = $UserGuidePath; ReleaseConfig = $httpConfig } } "http manifest url is refused" "https"
    Expect-Refusal { Invoke-Build @{ ManagerBinary = $goodBinary; Version = $version; OutputDirectory = (Join-Path $tempRoot "out-c2"); WebuiDirectory = $webui; UserGuidePath = $UserGuidePath; ReleaseConfig = $missingKeyConfig } } "missing public_key is refused" "public_key"
    Expect-Refusal { Invoke-Build @{ ManagerBinary = $goodBinary; Version = $version; OutputDirectory = (Join-Path $tempRoot "out-c3"); WebuiDirectory = $webui; UserGuidePath = $UserGuidePath; ReleaseConfig = $badChannelConfig } } "unknown channel is refused" "channel"
    Expect-Refusal { Invoke-Build @{ ManagerBinary = $goodBinary; Version = $version; OutputDirectory = (Join-Path $tempRoot "out-c4"); WebuiDirectory = $webui; UserGuidePath = $UserGuidePath; ReleaseConfig = $shortKeyConfig } } "wrong public_key length is refused" "32 bytes"
    Expect-Refusal { Invoke-Build @{ ManagerBinary = $goodBinary; Version = $version; OutputDirectory = (Join-Path $tempRoot "out-c5"); WebuiDirectory = $webui; UserGuidePath = $UserGuidePath; ReleaseConfig = $badBase64Config } } "non-base64 public_key is refused" "base64"
    Expect-Refusal { Invoke-Build @{ ManagerBinary = $goodBinary; Version = $version; OutputDirectory = (Join-Path $tempRoot "out-c6"); WebuiDirectory = $webui; UserGuidePath = $UserGuidePath; ReleaseConfig = $extraKeyConfig } } "extra release config key is refused" "listen"
    Expect-Refusal { Invoke-Build @{ ManagerBinary = $goodBinary; Version = $version; OutputDirectory = (Join-Path $tempRoot "out-r12"); WebuiDirectory = $webui; UserGuidePath = (Join-Path $tempRoot "missing-guide.md") } } "missing user guide is refused" "user-guide"
    Expect-Refusal { Invoke-Build @{ ManagerBinary = $goodBinary; Version = $version; OutputDirectory = $out; WebuiDirectory = $webui; UserGuidePath = $UserGuidePath; ReleaseConfig = $releaseConfig } } "existing zip is not overwritten" "already exists"

    # 6. -Force allows an intentional rebuild.
    $forced = Invoke-Build @{
        ManagerBinary = $goodBinary; Version = $version; VersionCode = 3; OutputDirectory = $out;
        WebuiDirectory = $webui; UserGuidePath = $UserGuidePath; ReleaseConfig = $releaseConfig; Force = $true
    }
    Check ($forced -like "MODULE_ZIP_OK*") "-Force rebuilds an existing preview zip"

    # 7. Formal -Release mode produces MaaZero-v<version>.zip and enforces release gates.
    $relOut = Join-Path $tempRoot "out-release"
    $relOutput = Invoke-Build @{
        ManagerBinary = $goodBinary; Version = "0.1.0"; VersionCode = 100; OutputDirectory = $relOut;
        WebuiDirectory = $webui; UserGuidePath = $UserGuidePath; ReleaseConfig = $releaseConfig; Release = $true
    }
    Check ($relOutput -like "MODULE_ZIP_OK*preview=false*") "-Release outputs MODULE_ZIP_OK with preview=false"
    $relZip = Join-Path $relOut "MaaZero-v0.1.0.zip"
    Check (Test-Path -LiteralPath $relZip -PathType Leaf) "-Release writes MaaZero-v0.1.0.zip"
    $relEntries = Read-ZipEntries $relZip
    Check ($relEntries.ContainsKey("README.md")) "-Release ships README.md inside zip"
    Check (-not $relEntries.ContainsKey("README-preview.md")) "-Release omits README-preview.md"
    $relProp = Get-Text $relEntries["module.prop"].Bytes
    Check ($relProp -match "(?m)^name=MaaZero$") "-Release sets name=MaaZero in module.prop"
    Check ($relProp -match "(?m)^version=0\.1\.0$") "-Release sets version=0.1.0 in module.prop"
    Check ($relProp -match "(?m)^versionCode=100$") "-Release sets versionCode=100 in module.prop"
    $relMeta = (Get-Text $relEntries["module-metadata.json"].Bytes) | ConvertFrom-Json
    CheckEqual $relMeta.preview $false "-Release sets metadata.preview to false"
    Expect-Refusal { Invoke-Build @{ ManagerBinary = $goodBinary; Version = "0.1.0"; OutputDirectory = (Join-Path $tempRoot "out-rel-noconf"); WebuiDirectory = $webui; UserGuidePath = $UserGuidePath; Release = $true } } "-Release without ReleaseConfig is refused" "ReleaseConfig"
    Expect-Refusal { Invoke-Build @{ ManagerBinary = $goodBinary; Version = "0.1.0-preview"; OutputDirectory = (Join-Path $tempRoot "out-rel-badver"); WebuiDirectory = $webui; UserGuidePath = $UserGuidePath; ReleaseConfig = $releaseConfig; Release = $true } } "-Release refuses preview version string" "SemVer"
} finally {
    if (Test-Path -LiteralPath $tempRoot) {
        $resolved = (Resolve-Path -LiteralPath $tempRoot).Path
        $tempBase = [System.IO.Path]::GetTempPath()
        if ($resolved.StartsWith($tempBase) -and (Split-Path -Leaf $resolved) -like "maa-module-test-*") {
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    }
}

if ($script:failed.Count -gt 0) {
    Write-Host "MODULE_PACKAGING_FAIL passed=$($script:passed) failed=$($script:failed.Count)"
    foreach ($failure in $script:failed) { Write-Host " - $failure" }
    exit 1
}

Write-Host "MODULE_PACKAGING_OK passed=$($script:passed)"
exit 0
