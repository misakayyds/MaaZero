#Requires -Version 5.1
<#
.SYNOPSIS
    生成可分发的 maa-phone-project 组件（扁平目录 + VERSION + inventory.json + project.sha256）。
.DESCRIPTION
    默认只读 packaging/project/source（内置待审查脚本快照，亦可通过 -Source 指定），输出到 -Out：

      -Out/project/*        扁平项目脚本；唯一允许的内容改写是 WebUI 绑定/端口
      -Out/VERSION          组件版本
      -Out/inventory.json   确定性清单：源校验和、打包校验和、改写记录、排除记录
      -Out/project.sha256   整包内容摘要（按 name\0sha256 规范串计算）

    规则：
      * 只收 .py/.sh/.js/.json，且必须位于源目录顶层（flat）；
      * 必需文件：maa-run.sh、maa-auto-update.sh、maa-webui.py；
      * 凭证（tg.txt、*.key、*.pem 等）出现即失败，绝不静默排除；
      * 设备专用脚本（start_webui.sh、ssh/wifi/charge/HAL/screen、adb_tcp、start_debian）、
        备份、日志、__pycache__、非白名单扩展名一律排除并记入 inventory；
      * 行尾统一 LF：CRLF 归一化并记录，孤立 CR 直接失败；
      * maa-webui.py 必须命中固定锚点，命中次数不为 1 即失败；
      * 不联网、不部署、不写时间戳，因此相同输入得到相同输出。

    生成的 WebUI 只绑定 127.0.0.1:8089，由 manager 在 8088 上做鉴权反代。
#>
[CmdletBinding()]
param(
    [string]$Source,
    [Parameter(Mandatory = $true)][string]$Out,
    [string]$Version = '0.0.0-dev',
    [switch]$Check,
    [string]$RepoRoot,
    [string]$ProjectFiles
)

$ErrorActionPreference = 'Stop'

trap {
    Write-Host ("BUILD_PROJECT_FAIL: " + $_.Exception.Message)
    exit 1
}

$script:Latin1 = [System.Text.Encoding]::GetEncoding(28591)
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$script:AllowedExtensions = @('.py', '.sh', '.js', '.json')
$script:RequiredFiles = @('maa-run.sh', 'maa-auto-update.sh', 'maa-webui.py')
$script:MaxFileBytes = 4MB
$script:MaxTotalBytes = 64MB
$script:MaxFiles = 4096
$script:NamePattern = '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
$script:IgnoredDirectories = @(
    '__pycache__',
    '_legacy_flat',
    'backups',
    'logs',
    'dist',
    'test',
    'node_modules',
    'maa-cli',
    'MAA-Meow',
    'MaaAssistantArknights',
    'arknights-mower',
    '.git'
)
$script:DeviceHelpers = @(
    'adb_tcp.sh',
    'charge_control.sh',
    'disable_face_hal.sh',
    'disable_fps_hal.sh',
    'maa_post_boot_screen_off.sh',
    'screen_backlight_off.sh',
    'screen_on.sh',
    'ssh_watchdog.sh',
    'ssh_watchdog_worker.sh',
    'start_debian.sh',
    'start_webui.sh',
    'wifi_enable_matrix_test.sh',
    'wifi_settings_switch_selftest.sh',
    'wifi_ui_recover.sh',
    'wifi_ui_recover_selftest.sh',
    'wifi_watchdog.sh',
    'wifi_watchdog_worker.sh'
)
$script:ArtifactPatterns = @(
    '(?i)\.bak($|[._-])',
    '(?i)(\.orig|\.rej|\.swp|\.tmp|\.log|\.pyc|\.pyo|~)$'
)
# 设备改动入口：不是新手默认能力，只排除不发布（Check 侧同样拒绝）。
$script:DeviceMutations = @(
    'maa-scheduled-reboot.sh',
    'maa-edit.sh'
)
$script:CredentialPatterns = @(
    '(?i)^tg\.txt$',
    '(?i)^\.env',
    '(?i)\.(pem|key|p12|pfx|netrc)$',
    '(?i)^id_rsa',
    '(?i)credential',
    '(?i)secret'
)
$script:ExcludedSourceFiles = @(
    'maa-edit.sh',
    'maa-scheduled-reboot.sh',
    'ssh_watchdog_worker.sh',
    'ssh_watchdog.sh',
    'start_webui.sh'
)

# 唯一允许的内容改写：把面向局域网的 WebUI 收回回环 + manager 私有上游端口。
$script:RequiredTransforms = @(
    @{
        Id   = 'webui-bind-loopback'
        File = 'maa-webui.py'
        From = 'server = WEB_SERVER_CLASS(("0.0.0.0", PORT), Handler)'
        To   = 'server = WEB_SERVER_CLASS(("127.0.0.1", PORT), Handler)'
    },
    @{
        Id   = 'webui-port-8089'
        File = 'maa-webui.py'
        From = 'PORT = 8088'
        To   = 'PORT = 8089'
    },
    @{
        Id   = 'webui-startup-log'
        File = 'maa-webui.py'
        From = 'http://0.0.0.0:{PORT}'
        To   = 'http://127.0.0.1:{PORT}'
    }
)

function Get-Sha256Hex {
    param([byte[]]$Bytes)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return (($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') }) -join '') }
    finally { $sha.Dispose() }
}

function Get-Occurrences {
    param([string]$Text, [string]$Needle)
    if ([string]::IsNullOrEmpty($Needle)) { return 0 }
    return ([regex]::Matches($Text, [regex]::Escape($Needle))).Count
}

function Test-AnyPattern {
    param([string]$Name, [string[]]$Patterns)
    foreach ($pattern in $Patterns) { if ($Name -match $pattern) { return $true } }
    return $false
}

function Write-LfFile {
    param([string]$Path, [string]$Content)
    [System.IO.File]::WriteAllText($Path, $Content, $script:Utf8NoBom)
    [System.IO.File]::SetLastWriteTimeUtc($Path, (New-Object datetime(2001, 1, 1, 0, 0, 0, ([System.DateTimeKind]::Utc))))
}

# 环境无关的字典序排序：Sort-Object 使用当前区域设置（NLS/ICU 不同），
# 会让同一份输入在 Windows PowerShell 5.1 与 PowerShell 7 上产生不同摘要。
function Sort-ByNameOrdinal {
    param([object[]]$Items)
    $list = @($Items)
    for ($i = 1; $i -lt $list.Count; $i++) {
        $item = $list[$i]
        $j = $i - 1
        while ($j -ge 0 -and ([string]::CompareOrdinal([string]$list[$j].Name, [string]$item.Name) -gt 0)) {
            $list[$j + 1] = $list[$j]
            $j--
        }
        $list[$j + 1] = $item
    }
    return , $list
}

# --- 参数与路径 -------------------------------------------------------
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path }
if (-not $Source) { $Source = Join-Path $PSScriptRoot 'source' }
if (-not $ProjectFiles) { $ProjectFiles = Join-Path $PSScriptRoot 'project-files.json' }
if ($Version -notmatch '^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$') { throw "版本号不合法: $Version" }
if (-not (Test-Path -LiteralPath $Source -PathType Container)) { throw "源目录不存在: $Source" }
if (-not (Test-Path -LiteralPath $ProjectFiles -PathType Leaf)) { throw "项目文件名单不存在: $ProjectFiles" }
$sourceItem = Get-Item -LiteralPath $Source -Force
if (($sourceItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "源根包含 reparse/link，拒绝打包: $Source" }

$allowlistDocument = $null
try { $allowlistDocument = Get-Content -LiteralPath $ProjectFiles -Raw | ConvertFrom-Json }
catch { throw "项目文件名单不是有效 JSON: $ProjectFiles" }
if ($null -eq $allowlistDocument -or $allowlistDocument.schema -ne 1) { throw '项目文件名单 schema 必须为 1' }
$allowlist = @($allowlistDocument.files)
if ($allowlist.Count -eq 0) { throw '项目文件名单不能为空' }
$allowlistedNames = @{}
foreach ($listedName in $allowlist) {
    if ($listedName -isnot [string] -or [string]::IsNullOrWhiteSpace($listedName)) { throw '项目文件名单包含非空字符串以外的条目' }
    if ($listedName -notmatch $script:NamePattern -or $listedName.Contains('/') -or $listedName.Contains('\') -or $listedName -eq '.' -or $listedName -eq '..') {
        throw "项目文件名单包含不安全路径: $listedName"
    }
    if ($allowlistedNames.ContainsKey($listedName)) { throw "项目文件名单包含重复条目: $listedName" }
    $allowlistedNames[$listedName] = $true
    if (Test-AnyPattern -Name $listedName -Patterns $script:CredentialPatterns) { throw "项目文件名单禁止凭证文件: $listedName" }
    if ($script:DeviceHelpers -contains $listedName -or $script:DeviceMutations -contains $listedName -or $script:ExcludedSourceFiles -contains $listedName) {
        throw "项目文件名单禁止设备危险或排除脚本: $listedName"
    }
    if ($script:AllowedExtensions -notcontains ([System.IO.Path]::GetExtension($listedName).ToLowerInvariant())) {
        throw "项目文件名单包含不支持的扩展名: $listedName"
    }
}
foreach ($required in $script:RequiredFiles) {
    if (-not $allowlistedNames.ContainsKey($required)) { throw "项目文件名单缺少必需文件: $required" }
}

$sourcePath = (Resolve-Path -LiteralPath $Source).Path.TrimEnd('\', '/')
$outPath = [System.IO.Path]::GetFullPath($Out).TrimEnd('\', '/')
if ($outPath -eq '') { throw '输出目录为空' }
if ($outPath -eq [System.IO.Path]::GetPathRoot($outPath).TrimEnd('\', '/')) { throw "输出目录不能是盘根: $outPath" }
if ($outPath -eq $sourcePath) { throw '输出目录不能等于源目录' }
if (-not $Check -and (Test-Path -LiteralPath $outPath)) { throw "输出目录已存在，拒绝覆盖: $outPath" }
$separator = [System.IO.Path]::DirectorySeparatorChar
if ($sourcePath.StartsWith($outPath + $separator, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "输出目录不能是源目录的父目录: $outPath"
}

# --- 枚举源目录 -------------------------------------------------------
$entries = New-Object System.Collections.Generic.List[object]
$excluded = New-Object System.Collections.Generic.List[object]
$totalBytes = 0
foreach ($item in (Sort-ByNameOrdinal -Items @(Get-ChildItem -LiteralPath $sourcePath -Force))) {
    if ($item.PSIsContainer) {
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "源目录包含 reparse/link，拒绝打包: $($item.Name)" }
        if ($script:IgnoredDirectories -contains $item.Name) {
            $excluded.Add(@{ Name = $item.Name; Reason = 'directory' })
            continue
        }
        throw "源目录必须保持扁平，发现子目录: $($item.Name)"
    }
    $name = $item.Name
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "源文件包含 reparse/link，拒绝打包: $name" }
    if (-not ($name -match $script:NamePattern)) { throw "文件名不安全，拒绝打包: $name" }
    if (Test-AnyPattern -Name $name -Patterns $script:CredentialPatterns) {
        throw "疑似凭证文件，禁止打包（请从权威源移除）: $name"
    }
    if ($script:DeviceHelpers -contains $name) {
        $excluded.Add(@{ Name = $name; Reason = 'device-helper' })
        continue
    }
    if ($script:DeviceMutations -contains $name) {
        $excluded.Add(@{ Name = $name; Reason = 'device-mutation' })
        continue
    }
    if ($script:ExcludedSourceFiles -contains $name) {
        $excluded.Add(@{ Name = $name; Reason = 'excluded-source-file' })
        continue
    }
    if (Test-AnyPattern -Name $name -Patterns $script:ArtifactPatterns) {
        $excluded.Add(@{ Name = $name; Reason = 'artifact' })
        continue
    }
    if ($script:AllowedExtensions -notcontains ([System.IO.Path]::GetExtension($name).ToLowerInvariant())) {
        $excluded.Add(@{ Name = $name; Reason = 'extension' })
        continue
    }
    if (-not $allowlistedNames.ContainsKey($name)) { throw "源文件未列入项目文件名单: $name" }
    $bytes = [System.IO.File]::ReadAllBytes($item.FullName)
    if ($bytes.Length -eq 0) { throw "项目文件为空: $name" }
    if ($bytes.Length -gt $script:MaxFileBytes) { throw "项目文件过大($($bytes.Length) 字节): $name" }
    $totalBytes += $bytes.Length
    if ($totalBytes -gt $script:MaxTotalBytes) { throw '项目总大小超出上限' }
    $entries.Add(@{ Name = $name; Bytes = $bytes })
    if ($entries.Count -gt $script:MaxFiles) { throw '项目文件数量超出上限' }
}

foreach ($required in $script:RequiredFiles) {
    if (-not ($entries | Where-Object { $_.Name -eq $required })) { throw "缺少名单文件: $required" }
}
foreach ($listedName in $allowlist) {
    if (-not ($entries | Where-Object { $_.Name -eq $listedName })) { throw "缺少名单文件: $listedName" }
}

# --- 行尾归一化 + WebUI 改写 ------------------------------------------
$files = New-Object System.Collections.Generic.List[object]
$appliedTransforms = New-Object System.Collections.Generic.List[object]
foreach ($entry in $entries) {
    $name = $entry.Name
    $sourceSha = Get-Sha256Hex -Bytes $entry.Bytes
    $text = $script:Latin1.GetString($entry.Bytes)
    $transforms = New-Object System.Collections.Generic.List[string]
    if ($text.Contains("`r`n")) {
        $text = $text.Replace("`r`n", "`n")
        $transforms.Add('normalize-lf')
    }
    if ($text.Contains("`r")) { throw "文件含孤立 CR 字节: $name" }
    foreach ($transform in $script:RequiredTransforms) {
        if ($transform.File -ne $name) { continue }
        $count = Get-Occurrences -Text $text -Needle $transform.From
        if ($count -ne 1) {
            throw "WebUI 锚点不匹配，拒绝猜测上游改动: $($transform.Id) 期望 1 次，实际 $count 次（anchor: $($transform.From)）"
        }
        $text = $text.Replace($transform.From, $transform.To)
        $transforms.Add($transform.Id)
        $appliedTransforms.Add(@{ Id = $transform.Id; File = $name; From = $transform.From; To = $transform.To })
    }
    if ($name -eq 'maa-webui.py') {
        if ($text.Contains('0.0.0.0')) { throw '改写后的 maa-webui.py 仍含 0.0.0.0' }
        if ((Get-Occurrences -Text $text -Needle 'PORT = 8089') -ne 1) { throw '改写后的 maa-webui.py 必须恰好声明一次 PORT = 8089' }
        if ((Get-Occurrences -Text $text -Needle 'server = WEB_SERVER_CLASS(("127.0.0.1", PORT), Handler)') -ne 1) { throw '改写后的 maa-webui.py 必须恰好绑定一次回环地址' }
    }
    $packedBytes = $script:Latin1.GetBytes($text)
    $files.Add(@{
            Name         = $name
            Size         = $packedBytes.Length
            Sha256       = Get-Sha256Hex -Bytes $packedBytes
            SourceSha256 = $sourceSha
            Transforms   = @($transforms.ToArray())
            Bytes        = $packedBytes
        })
}
$files = Sort-ByNameOrdinal -Items $files.ToArray()
$excluded = Sort-ByNameOrdinal -Items $excluded.ToArray()

$canonical = ($files | ForEach-Object { "$($_.Name)`0$($_.Sha256)" }) -join "`n"
$projectSha = Get-Sha256Hex -Bytes ([System.Text.Encoding]::UTF8.GetBytes($canonical))

# --- 写盘（-Check 只校验）--------------------------------------------
if (-not $Check) {
    if (-not (Test-Path -LiteralPath $outPath)) { New-Item -ItemType Directory -Force -Path $outPath | Out-Null }
    $projectOut = Join-Path $outPath 'project'
    New-Item -ItemType Directory -Force -Path $projectOut | Out-Null
    foreach ($file in $files) {
        $dest = Join-Path $projectOut $file.Name
        [System.IO.File]::WriteAllBytes($dest, $file.Bytes)
        [System.IO.File]::SetLastWriteTimeUtc($dest, (New-Object datetime(2001, 1, 1, 0, 0, 0, ([System.DateTimeKind]::Utc))))
    }

    $inventoryLines = New-Object System.Collections.Generic.List[string]
    $inventoryLines.Add('{')
    $inventoryLines.Add('  "schema": 1,')
    $inventoryLines.Add('  "component": "maa-phone-project",')
    $inventoryLines.Add("  `"version`": `"$Version`",")
    $inventoryLines.Add("  `"projectSha256`": `"$projectSha`",")
    $inventoryLines.Add('  "contract": {')
    $inventoryLines.Add('    "webuiListen": "127.0.0.1:8089",')
    $inventoryLines.Add('    "managerPublicPort": 8088,')
    $inventoryLines.Add('    "taskLock": "/tmp/maa-task.lock",')
    $inventoryLines.Add('    "projectDirInsideRootfs": "/root/maa-scripts"')
    $inventoryLines.Add('  },')
    $inventoryLines.Add('  "files": [')
    for ($i = 0; $i -lt $files.Count; $i++) {
        $file = $files[$i]
        $suffix = if ($i -lt $files.Count - 1) { ',' } else { '' }
        $transformNames = ($file.Transforms | ForEach-Object { '"' + $_ + '"' }) -join ', '
        $inventoryLines.Add('    {"name": "' + $file.Name + '", "size": ' + $file.Size + ', "sha256": "' + $file.Sha256 + '", "sourceSha256": "' + $file.SourceSha256 + '", "transforms": [' + $transformNames + ']}' + $suffix)
    }
    $inventoryLines.Add('  ],')
    $inventoryLines.Add('  "transforms": [')
    for ($i = 0; $i -lt $appliedTransforms.Count; $i++) {
        $transform = $appliedTransforms[$i]
        $suffix = if ($i -lt $appliedTransforms.Count - 1) { ',' } else { '' }
        $inventoryLines.Add('    {"id": "' + $transform.Id + '", "file": "' + $transform.File + '", "from": "' + $transform.From.Replace('"', '\"') + '", "to": "' + $transform.To.Replace('"', '\"') + '"}' + $suffix)
    }
    $inventoryLines.Add('  ],')
    $inventoryLines.Add('  "excluded": [')
    for ($i = 0; $i -lt $excluded.Count; $i++) {
        $skip = $excluded[$i]
        $suffix = if ($i -lt $excluded.Count - 1) { ',' } else { '' }
        $inventoryLines.Add('    {"name": "' + $skip.Name + '", "reason": "' + $skip.Reason + '"}' + $suffix)
    }
    $inventoryLines.Add('  ]')
    $inventoryLines.Add('}')

    Write-LfFile (Join-Path $outPath 'VERSION') ("$Version`n")
    Write-LfFile (Join-Path $outPath 'inventory.json') (($inventoryLines -join "`n") + "`n")
    Write-LfFile (Join-Path $outPath 'project.sha256') ("$projectSha  project`n")
}

$summary = "files=$($files.Count) excluded=$($excluded.Count) sha256=$projectSha"
if ($Check) {
    Write-Host "BUILD_PROJECT_CHECK_OK $summary version=$Version"
    exit 0
}

Write-Host "BUILD_PROJECT_OK $summary version=$Version out=$outPath"
exit 0
