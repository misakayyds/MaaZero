#Requires -Version 5.1
<#
.SYNOPSIS
    Build a local preview module ZIP for MAA Phone Manager.
.DESCRIPTION
    Refuses anything that is not an explicitly named preview build:
      * the ARM64 manager binary must exist and be a static 64-bit AArch64 ELF,
      * -Version must look like 0.1.0-preview (a release-looking version is refused),
      * an optional public release.json may contain only manifest_url, public_key and
        channel, must be <= 8 KiB, use an https manifest_url and a 32-byte base64 key,
      * module-settings.js/.css and docs/user-guide.md must be present.

    The ZIP contains only an allowlist of module files plus the supplied binary,
    the optional public config, the offline user guide and a generated local
    tutorial. It never packages the repository, credentials or user data, and it
    normalises every text file to LF. This script never installs anything.
#>
[CmdletBinding()]
param(
    [string]$ManagerBinary = "",
    [string]$ReleaseConfig = "",
    [string]$Version = "",
    [int]$VersionCode = 1,
    [string]$OutputDirectory = "",
    [string]$WebuiDirectory = "",
    [string]$UserGuidePath = "",
    [string]$BrandName = "MaaZero",
    [string]$Author = "misakayyds",
    [switch]$Release,
    [switch]$AllowMissingWebuiAssets,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

function Fail {
    param([string]$Message)
    throw "build-module: $Message"
}

function Resolve-ExistingPath {
    param([string]$Path, [string]$Name)
    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $resolved) { Fail "$Name not found: $Path" }
    return $resolved.Path
}

$moduleSource = Resolve-ExistingPath (Join-Path (Join-Path $PSScriptRoot "..") "module") "module source tree"
if ($OutputDirectory.Length -eq 0) { $OutputDirectory = Join-Path (Join-Path $PSScriptRoot "..") "dist" }
if ($WebuiDirectory.Length -eq 0) { $WebuiDirectory = Join-Path (Join-Path $PSScriptRoot "..") "webui" }
if ($UserGuidePath.Length -eq 0) { $UserGuidePath = Join-Path (Join-Path (Join-Path $PSScriptRoot "..") "docs") "user-guide.md" }

$minimumVersion = "1"
if ($Release) {
    if ($Version -notmatch "^\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)(?:\.[0-9]+)?)?$") {
        Fail "release version must follow SemVer such as 0.1.0 or 0.1.0-rc.1 (got [$Version])"
    }
    if ($AllowMissingWebuiAssets) {
        Fail "formal release builds must include webui assets (-AllowMissingWebuiAssets is forbidden with -Release)"
    }
    if ($BrandName -notmatch "^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$") {
        Fail "invalid BrandName [$BrandName]"
    }
} else {
    if ($Version -notmatch "^\d+\.\d+\.\d+-preview(\.[0-9]+)?$") {
        Fail "version must be an explicit preview version such as 0.1.0-preview (got [$Version])"
    }
}
if ($VersionCode -lt 1) { Fail "versionCode must be >= 1 (got $VersionCode)" }

if ($ManagerBinary.Length -eq 0) { Fail "manager binary is required (module/bin/maa-managerd)" }
if ($ManagerBinary -like "*.exe") {
    Fail "manager binary must be the ARM64 Android build, not a Windows executable (.exe): $ManagerBinary"
}
$ManagerBinary = Resolve-ExistingPath $ManagerBinary "manager binary (module/bin/maa-managerd)"
$binaryBytes = [System.IO.File]::ReadAllBytes($ManagerBinary)
if ($binaryBytes.Length -eq 0) { Fail "manager binary is empty: $ManagerBinary" }

if ($binaryBytes.Length -lt 64) { Fail "manager binary is too small to be an ELF file: $ManagerBinary" }
if (-not ($binaryBytes[0] -eq 0x7F -and $binaryBytes[1] -eq 0x45 -and $binaryBytes[2] -eq 0x4C -and $binaryBytes[3] -eq 0x46)) {
    Fail "manager binary is not an ELF file (bad magic): $ManagerBinary"
}
if ($binaryBytes[5] -ne 1) { Fail "manager binary must be a little-endian ELF: $ManagerBinary" }
if ($binaryBytes[4] -ne 2) { Fail "manager binary must be a 64-bit ELF (EI_CLASS=$($binaryBytes[4])): $ManagerBinary" }
$machine = [BitConverter]::ToUInt16($binaryBytes, 18)
if ($machine -ne 0xB7) {
    Fail ("manager binary must be an AArch64/arm64 ELF (e_machine=0x{0:X4}): {1}" -f $machine, $ManagerBinary)
}
$elfType = [BitConverter]::ToUInt16($binaryBytes, 16)
if ($elfType -ne 2 -and $elfType -ne 3) {
    Fail ("manager binary has an unsupported ELF type ({0}): {1}" -f $elfType, $ManagerBinary)
}
$programHeaderOffset = [int][BitConverter]::ToUInt64($binaryBytes, 32)
$programHeaderSize = [int][BitConverter]::ToUInt16($binaryBytes, 54)
$programHeaderCount = [int][BitConverter]::ToUInt16($binaryBytes, 56)
for ($index = 0; $index -lt $programHeaderCount; $index++) {
    $offset = $programHeaderOffset + ($index * $programHeaderSize)
    if ($offset -lt 0 -or ($offset + 4) -gt $binaryBytes.Length) {
        Fail "manager binary has a truncated program header table: $ManagerBinary"
    }
    if ([BitConverter]::ToUInt32($binaryBytes, $offset) -eq 3) {
        Fail "manager binary is dynamically linked (PT_INTERP); build it with CGO_ENABLED=0: $ManagerBinary"
    }
}

$releaseIncluded = $false
if ($ReleaseConfig.Length -gt 0) {
    $ReleaseConfig = Resolve-ExistingPath $ReleaseConfig "public release config (release.json)"
    $configBytes = [System.IO.File]::ReadAllBytes($ReleaseConfig)
    if ($configBytes.Length -gt 8192) {
        Fail "release config size $($configBytes.Length) bytes exceeds the 8192 byte limit (public bootstrap config only)"
    }
    $configText = [System.Text.Encoding]::UTF8.GetString($configBytes)
    try {
        $configObject = ConvertFrom-Json -InputObject $configText
    } catch {
        Fail "release config is not valid JSON: $($_.Exception.Message)"
    }
    if ($configObject -isnot [System.Management.Automation.PSCustomObject]) {
        Fail "release config must be a JSON object (public bootstrap configuration only)"
    }
    # Strictly aligned with the daemon CLI: the public config has exactly these keys.
    $allowedConfigKeys = @("manifest_url", "public_key", "channel")
    foreach ($property in $configObject.PSObject.Properties) {
        if ($allowedConfigKeys -notcontains $property.Name) {
            Fail "release config only allows manifest_url, public_key and channel (unknown key [$($property.Name)])"
        }
    }
    foreach ($requiredKey in $allowedConfigKeys) {
        if ($null -eq $configObject.PSObject.Properties[$requiredKey]) {
            Fail "release config is missing the required key [$requiredKey]"
        }
    }
    $manifestUrl = $configObject.manifest_url
    if ($manifestUrl -isnot [string]) { Fail "release config manifest_url must be a string" }
    $manifestUri = $null
    if (-not [System.Uri]::TryCreate([string]$manifestUrl, [System.UriKind]::Absolute, [ref]$manifestUri)) {
        Fail "release config manifest_url must be an absolute URL (got [$manifestUrl])"
    }
    if ($manifestUri.Scheme -ne "https") {
        Fail "release config manifest_url must use https (got [$manifestUrl])"
    }
    if ($manifestUri.UserInfo.Length -gt 0) {
        Fail "release config manifest_url must not embed credentials"
    }
    $channel = $configObject.channel
    if ($channel -isnot [string] -or ($channel -ne "stable" -and $channel -ne "beta")) {
        Fail "release config channel must be stable or beta (got [$channel])"
    }
    $publicKey = $configObject.public_key
    if ($publicKey -isnot [string]) { Fail "release config public_key must be a base64 string" }
    $publicKeyBytes = $null
    try {
        $publicKeyBytes = [Convert]::FromBase64String([string]$publicKey)
    } catch {
        Fail "release config public_key must be valid base64: $($_.Exception.Message)"
    }
    if ($publicKeyBytes.Length -ne 32) {
        Fail "release config public_key must decode to exactly 32 bytes (got $($publicKeyBytes.Length))"
    }
    $releaseIncluded = $true
}
if ($Release -and -not $releaseIncluded) {
    Fail "formal release builds require -ReleaseConfig pointing to a public release.json"
}

$UserGuidePath = Resolve-ExistingPath $UserGuidePath "offline user guide (docs/user-guide.md)"

$webuiAssets = @("module-settings.js", "module-settings.css")
$webuiSource = @{}
$missingAssets = New-Object "System.Collections.Generic.List[string]"
foreach ($asset in $webuiAssets) {
    $candidate = Join-Path $WebuiDirectory $asset
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { $webuiSource[$asset] = (Resolve-Path -LiteralPath $candidate).Path }
    else { $missingAssets.Add($asset) }
}
$webuiIncluded = ($missingAssets.Count -eq 0)
if (-not $webuiIncluded -and -not $AllowMissingWebuiAssets) {
    Fail ("webui assets are missing: " + (($missingAssets | ForEach-Object { "webui/$_" }) -join ", ") + " under $WebuiDirectory; pass -AllowMissingWebuiAssets for a local preview build")
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$OutputDirectory = (Resolve-Path -LiteralPath $OutputDirectory).Path
$zipFileName = if ($Release) { $BrandName + "-v" + $Version + ".zip" } else { "MAA-Phone-Manager-preview-" + $Version + ".zip" }
$zipPath = Join-Path $OutputDirectory $zipFileName
if ((Test-Path -LiteralPath $zipPath) -and -not $Force) {
    Fail "output zip already exists: $zipPath (pass -Force to overwrite it)"
}

$textExtensions = @(".sh", ".prop", ".md", ".json", ".html", ".js", ".css", ".txt")

function Write-NormalizedText {
    param([string]$Path, [string]$SourcePath)
    $text = [System.IO.File]::ReadAllText($SourcePath, [System.Text.Encoding]::UTF8)
    $text = $text.Replace("`r`n", "`n").Replace("`r", "`n")
    if (-not $text.EndsWith("`n")) { $text = $text + "`n" }
    [System.IO.File]::WriteAllText($Path, $text, (New-Object "System.Text.UTF8Encoding" $false))
}

$stagingRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("maa-module-build-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $stagingRoot | Out-Null
$stagingBase = (Resolve-Path -LiteralPath $stagingRoot).Path

try {
    $staged = New-Object "System.Collections.Generic.List[object]"

    function Add-StagedFile {
        param([string]$RelativePath, [string]$SourcePath, [string]$Mode, [byte[]]$RawBytes = $null)
        $target = Join-Path $stagingBase ($RelativePath -replace "/", [string][System.IO.Path]::DirectorySeparatorChar)
        $targetDirectory = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $targetDirectory)) { New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null }
        $extension = [System.IO.Path]::GetExtension($RelativePath).ToLowerInvariant()
        if ($textExtensions -contains $extension) {
            Write-NormalizedText -Path $target -SourcePath $SourcePath
        } elseif ($null -ne $RawBytes) {
            [System.IO.File]::WriteAllBytes($target, $RawBytes)
        } else {
            [System.IO.File]::Copy($SourcePath, $target, $true)
        }
        $staged.Add([pscustomobject]@{ Name = $RelativePath; Path = $target; Mode = $Mode })
    }

    $moduleFileAllowlist = @("module.prop", "customize.sh", "service.sh", "action.sh", "uninstall.sh")
    foreach ($name in $moduleFileAllowlist) {
        $source = Join-Path $moduleSource $name
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { Fail "module source is missing $name" }
        $mode = "0755"
        if ($name -eq "module.prop") { $mode = "0644" }
        Add-StagedFile -RelativePath $name -SourcePath $source -Mode $mode
    }

    $webrootSource = Join-Path $moduleSource "webroot"
    if (-not (Test-Path -LiteralPath $webrootSource -PathType Container)) { Fail "module source is missing webroot/" }
    if (Test-Path -LiteralPath (Join-Path $webrootSource "assets")) {
        Fail "module source must not contain webroot/assets; the packager supplies it"
    }
    foreach ($file in (Get-ChildItem -LiteralPath $webrootSource -Recurse -File)) {
        $relative = "webroot/" + $file.FullName.Substring($webrootSource.Length).TrimStart([char]92, [char]47).Replace([string][System.IO.Path]::DirectorySeparatorChar, "/")
        $mode = "0644"
        if ([System.IO.Path]::GetExtension($file.Name).ToLowerInvariant() -eq ".sh") { $mode = "0755" }
        Add-StagedFile -RelativePath $relative -SourcePath $file.FullName -Mode $mode
    }

    Add-StagedFile -RelativePath "bin/maa-managerd" -SourcePath $ManagerBinary -Mode "0755" -RawBytes $binaryBytes

    if ($releaseIncluded) { Add-StagedFile -RelativePath "release.json" -SourcePath $ReleaseConfig -Mode "0644" }

    foreach ($asset in $webuiAssets) {
        if ($webuiSource.ContainsKey($asset)) {
            Add-StagedFile -RelativePath ("webroot/assets/" + $asset) -SourcePath $webuiSource[$asset] -Mode "0644"
        }
    }
    Add-StagedFile -RelativePath "webroot/assets/user-guide.md" -SourcePath $UserGuidePath -Mode "0644"

    # module.prop carries the requested build version and, in -Release mode, formal MaaZero metadata.
    $propSource = Join-Path $stagingBase "module.prop"
    $propLines = New-Object "System.Collections.Generic.List[string]"
    foreach ($line in ([System.IO.File]::ReadAllText($propSource, [System.Text.Encoding]::UTF8) -split "`n")) {
        $trimmed = $line.TrimEnd()
        if ($trimmed.Length -eq 0) { continue }
        if ($Release -and ($trimmed -like "name=*")) { $propLines.Add("name=$BrandName"); continue }
        if ($trimmed -like "version=*") { $propLines.Add("version=$Version"); continue }
        if ($trimmed -like "versionCode=*") { $propLines.Add("versionCode=$VersionCode"); continue }
        if ($Release -and ($trimmed -like "author=*")) { $propLines.Add("author=$Author"); continue }
        if ($Release -and ($trimmed -like "description=*")) {
            $propLines.Add("description=零电脑依赖、零命令行折腾、零系统接管的手机端 MAA 独立沙箱模块（支持 KernelSU / Magisk / APatch）。")
            continue
        }
        $propLines.Add($trimmed)
    }
    [System.IO.File]::WriteAllText($propSource, (($propLines -join "`n") + "`n"), (New-Object "System.Text.UTF8Encoding" $false))

    $managerHash = (Get-FileHash -LiteralPath $ManagerBinary -Algorithm SHA256).Hash.ToLowerInvariant()
    $webuiAssetsState = "missing"
    if ($webuiIncluded) { $webuiAssetsState = "included" }
    $releaseState = "absent"
    if ($releaseIncluded) { $releaseState = "included" }
    if ($Release) {
        $readmeDocName = "README.md"
        $tutorialLines = @(
            "# $BrandName v$Version - Android Root Module",
            "",
            "零电脑依赖、零命令行折腾、零系统接管的手机端 MAA 独立沙箱编排模块。",
            "",
            "Offline user guide (Chinese): webroot/assets/user-guide.md",
            "",
            "## Included",
            "",
            "- bin/maa-managerd: static ARM64 daemon (SHA-256 $managerHash).",
            "- release.json: Ed25519 public verification key and signed HTTPS channel manifest URL.",
            "- service.sh: starts maa-managerd on 127.0.0.1:8088 after boot.",
            "- action.sh & webroot/: opens the local management WebUI from KernelSU / Magisk / APatch.",
            "- uninstall.sh: stops background services, unmounts chroot cleanly, and preserves user data.",
            "",
            "## Data directory",
            "",
            "All runtime files, MaaCore resources, configs, and logs reside in /data/local/maa-platform.",
            "Uninstalling this module never deletes /data/local/maa-platform."
        )
    } else {
        $readmeDocName = "README-preview.md"
        $tutorialLines = @(
            "# MAA Phone Manager - local preview module",
            "",
            "This is a *preview* build: it only demonstrates the module layout locally and has",
            "NOT passed device acceptance. Do not treat it as a release package.",
            "",
            "Offline user guide (Chinese): webroot/assets/user-guide.md",
            "",
            "## Included",
            "",
            "- bin/maa-managerd: static ARM64 daemon supplied with -ManagerBinary (SHA-256 $managerHash).",
            "- service.sh: starts maa-managerd serve in the background, listening on 127.0.0.1:8088.",
            "- action.sh: Magisk module action button; calls the fixed open-ui command.",
            "- uninstall.sh: calls stop and keeps all user data.",
            "- webroot/: KernelSU/APatch module page; it only runs the fixed open-ui command and",
            "  offers no arbitrary command input.",
            "- webroot/assets/: module settings assets plus the offline user guide.",
            "",
            "## Not included",
            "",
            "- No Debian rootfs, MaaCore, game resources or user data.",
            "- No credentials, notification config, logs, screenshots or device backups.",
            "- No system partition changes; nothing outside the module directory is written.",
            "",
            "## Data directory",
            "",
            "Runtime data lives in /data/local/maa-platform. Uninstalling the module stops the",
            "daemon and keeps that directory.",
            "",
            "## Build and verify locally",
            "",
            "  powershell -ExecutionPolicy Bypass -File .\packaging\build-module.ps1 -ManagerBinary <arm64-static-binary> -Version " + $Version + " -VersionCode " + $VersionCode,
            "  powershell -ExecutionPolicy Bypass -File .\packaging\test-module.ps1",
            "",
            "The packaging script never installs the module, never connects to a phone and never",
            "writes user data."
        )
    }
    $tutorialPath = Join-Path $stagingBase $readmeDocName
    [System.IO.File]::WriteAllText($tutorialPath, (($tutorialLines -join "`n") + "`n"), (New-Object "System.Text.UTF8Encoding" $false))
    $staged.Add([pscustomobject]@{ Name = $readmeDocName; Path = $tutorialPath; Mode = "0644" })

    $inventory = New-Object "System.Collections.Generic.List[object]"
    foreach ($entry in ($staged | Sort-Object Name)) {
        $hash = (Get-FileHash -LiteralPath $entry.Path -Algorithm SHA256).Hash.ToLowerInvariant()
        $size = (Get-Item -LiteralPath $entry.Path).Length
        $isText = $textExtensions -contains [System.IO.Path]::GetExtension($entry.Name).ToLowerInvariant()
        $inventory.Add([ordered]@{
                path = $entry.Name
                sha256 = $hash
                size = $size
                mode = $entry.Mode
                lf = $isText
            })
    }

    $isPreview = (-not $Release)
    $metadata = [ordered]@{
        schema_version = 1
        preview = $isPreview
        brand = $BrandName
        module_id = "maa-phone-manager"
        version = $Version
        version_code = $VersionCode
        target_architecture = "arm64-v8a"
        minimum_android_api = 30
        data_root = "/data/local/maa-platform"
        listen = "127.0.0.1:8088"
        manager = [ordered]@{
            path = "bin/maa-managerd"
            architecture = "arm64-v8a"
            linked = "static"
            sha256 = $managerHash
            size = $binaryBytes.Length
        }
        webui_assets = $webuiAssetsState
        release_config = $releaseState
        generated_by = "maa-phone-manager/packaging/build-module.ps1"
        lf_normalized = $true
        files = $inventory
    }
    $metadataPath = Join-Path $stagingBase "module-metadata.json"
    $metadataText = (ConvertTo-Json -InputObject $metadata -Depth 6) -replace "`r`n", "`n"
    $metadataText = $metadataText -replace "`r", "`n"
    [System.IO.File]::WriteAllText($metadataPath, ($metadataText + "`n"), (New-Object "System.Text.UTF8Encoding" $false))
    $staged.Add([pscustomobject]@{ Name = "module-metadata.json"; Path = $metadataPath; Mode = "0644" })

    # Deterministic ZIP: sorted entries, fixed timestamps, unix modes.
    Add-Type -AssemblyName System.IO.Compression | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem | Out-Null
    $zipStream = [System.IO.File]::Open($zipPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
        $archive = New-Object "System.IO.Compression.ZipArchive" -ArgumentList $zipStream, ([System.IO.Compression.ZipArchiveMode]::Create)
        try {
            foreach ($entry in ($staged | Sort-Object Name)) {
                $zipEntry = $archive.CreateEntry($entry.Name, [System.IO.Compression.CompressionLevel]::Optimal)
                $zipEntry.LastWriteTime = [System.DateTimeOffset]::new(1980, 1, 1, 0, 0, 0, [System.TimeSpan]::Zero)
                $unixMode = [Convert]::ToInt32($entry.Mode, 8)
                $zipEntry.ExternalAttributes = ($unixMode -shl 16) -bor 0x20
                $target = $zipEntry.Open()
                try {
                    $bytes = [System.IO.File]::ReadAllBytes($entry.Path)
                    $target.Write($bytes, 0, $bytes.Length)
                } finally { $target.Dispose() }
            }
        } finally { $archive.Dispose() }
    } finally { $zipStream.Dispose() }

    # Verify what was written before reporting success.
    $verifyArchive = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        $names = @($verifyArchive.Entries | ForEach-Object { $_.FullName })
        if ($names.Count -ne $staged.Count) { Fail "zip verification failed: entry count $($names.Count) != $($staged.Count)" }
        foreach ($entry in $verifyArchive.Entries) {
            $stream = $entry.Open()
            $memory = New-Object "System.IO.MemoryStream"
            try { $stream.CopyTo($memory) } finally { $stream.Dispose() }
            $bytes = $memory.ToArray()
            $memory.Dispose()
            if ($bytes.Length -eq 0) { Fail "zip verification failed: $($entry.FullName) is empty" }
            $extension = [System.IO.Path]::GetExtension($entry.FullName).ToLowerInvariant()
            if ($textExtensions -contains $extension -and ($bytes -contains 13)) {
                Fail "zip verification failed: $($entry.FullName) is not LF normalised"
            }
        }
    } finally { $verifyArchive.Dispose() }

    $zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if (-not $Release) {
        Write-Host "MODULE_PREVIEW_WARNING: this package is a local preview build; it is never installed by this tooling."
        if (-not $webuiIncluded) {
            Write-Host "MODULE_PREVIEW_WARNING: webroot/assets/module-settings.js|css are missing; the packaged UI assets are incomplete."
        }
        if (-not $releaseIncluded) {
            Write-Host "MODULE_PREVIEW_WARNING: no release.json supplied; the daemon starts in recovery-only mode."
        }
    }
    $previewFlagText = if ($Release) { "false" } else { "true" }
    Write-Output ("MODULE_ZIP_OK path=" + $zipPath + " sha256=" + $zipHash + " entries=" + $staged.Count + " version=" + $Version + " versionCode=" + $VersionCode + " preview=" + $previewFlagText)
} finally {
    if (Test-Path -LiteralPath $stagingBase) {
        $tempBase = [System.IO.Path]::GetTempPath()
        if ($stagingBase.StartsWith($tempBase) -and (Split-Path -Leaf $stagingBase) -like "maa-module-build-*") {
            Remove-Item -LiteralPath $stagingBase -Recurse -Force
        }
    }
}
