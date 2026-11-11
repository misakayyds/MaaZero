#Requires -Version 5.1
<#
.SYNOPSIS
    build-project.ps1 的本地测试：只写临时目录，不连接手机、不部署、不改权威源。
.DESCRIPTION
    覆盖 allowlist/排除规则、凭证与行尾拒绝、WebUI 锚点不匹配即失败、确定性输出、
    改写后校验和记录，以及用真实结构化源跑一次只读构建。
#>
[CmdletBinding()]
param(
    [string]$RepoRoot
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path }
$checks = 0
$failures = New-Object "System.Collections.Generic.List[string]"
$buildScript = Join-Path $PSScriptRoot "..\build-project.ps1"
$utf8 = New-Object System.Text.UTF8Encoding($false)

$webuiPublicBind = 'server = WEB_SERVER_CLASS(("0.0.0.0", PORT), Handler)'
$webuiLoopbackBind = 'server = WEB_SERVER_CLASS(("127.0.0.1", PORT), Handler)'

function Assert-That {
    param([bool]$Condition, [string]$Message)
    $script:checks++
    if (-not $Condition) { $script:failures.Add($Message) }
}

function Write-LfFile {
    param([string]$Path, [string]$Content)
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    [System.IO.File]::WriteAllText($Path, $Content, $utf8)
}

function Get-Sha256Hex {
    param([string]$Path)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return (($sha.ComputeHash([System.IO.File]::ReadAllBytes($Path)) | ForEach-Object { $_.ToString("x2") }) -join "") }
    finally { $sha.Dispose() }
}

function Read-Text {
    param([string]$Path)
    return [System.IO.File]::ReadAllText($Path)
}

function Invoke-Build {
    param([string[]]$Arguments)
    # 数组按 -Name value 成对解析成哈希表，避免把参数当成位置参数传给构建脚本。
    $splat = @{}
    for ($i = 0; $i -lt $Arguments.Count; $i++) {
        $token = [string]$Arguments[$i]
        if (-not $token.StartsWith('-')) { throw "Invoke-Build 参数格式错误: $token" }
        $name = $token.TrimStart('-')
        if (($i + 1) -lt $Arguments.Count -and -not ([string]$Arguments[$i + 1]).StartsWith('-')) {
            $splat[$name] = $Arguments[$i + 1]
            $i++
        }
        else {
            $splat[$name] = $true
        }
    }
    if (-not $splat.ContainsKey('ProjectFiles') -and $splat.ContainsKey('Source')) {
        $fixtureManifest = Join-Path (Split-Path -Parent ([string]$splat['Source'])) 'project-files.json'
        if (Test-Path -LiteralPath $fixtureManifest -PathType Leaf) { $splat['ProjectFiles'] = $fixtureManifest }
    }
    $output = & $buildScript @splat *>&1 | Out-String
    return @{ ExitCode = $LASTEXITCODE; Output = $output }
}

function New-TempCase {
    $root = Join-Path ([System.IO.Path]::GetTempPath()) ("maa-project-test-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    return $root
}

function New-FixtureSource {
    param([string]$Root)
    $src = Join-Path $Root "root_maa_scripts"
    Write-LfFile (Join-Path $src "maa-run.sh") ((@('#!/bin/bash', 'LOCK_FILE="/tmp/maa-task.lock"') -join "`n") + "`n")
    Write-LfFile (Join-Path $src "maa-auto-update.sh") ((@('#!/bin/bash', 'echo update') -join "`n") + "`n")
    Write-LfFile (Join-Path $src "maa-webui.py") ((@('#!/usr/bin/env python3', 'PORT = 8088', $webuiPublicBind, 'print(f"start http://0.0.0.0:{PORT}")') -join "`n") + "`n")
    Write-LfFile (Join-Path $src "maa-weekly-editor.js") "// editor`n"
    Write-LfFile (Join-Path $src "infrast-operator-requirements.json") "{`"schema`":1}`n"
    Write-LfFile (Join-Path $src "maa-crlf-helper.sh") "#!/bin/bash`necho helper`n"
    Write-LfFile (Join-Path $src "start_webui.sh") ((@('#!/bin/bash', 'pkill -f maa-webui.py') -join "`n") + "`n")
    Write-LfFile (Join-Path $src "maa-scheduled-reboot.sh") ((@('#!/bin/bash', 'echo reboot') -join "`n") + "`n")
    Write-LfFile (Join-Path $src "arknights-apk-update.sh") ((@('#!/bin/bash', 'LOCK_FILE="/tmp/maa-task.lock"') -join "`n") + "`n")
    Write-LfFile (Join-Path $src "maa-edit.sh") ((@('#!/bin/bash', 'echo edit') -join "`n") + "`n")
    Write-LfFile (Join-Path $src "README.md") "notes`n"
    Write-LfFile (Join-Path $src "maa-run.sh.bak-20260101") "#!/bin/bash`n"
    Write-LfFile (Join-Path $src "__pycache__\maa-webui.cpython-312.pyc") "bytecode"
    Write-LfFile (Join-Path $src "_legacy_flat\maa-run.sh") "#!/bin/bash`nLOCK_FILE=`"/tmp/old.lock`"`n"
    Write-LfFile (Join-Path $Root "project-files.json") (([ordered]@{ schema = 1; files = @('arknights-apk-update.sh', 'infrast-operator-requirements.json', 'maa-auto-update.sh', 'maa-crlf-helper.sh', 'maa-run.sh', 'maa-webui.py', 'maa-weekly-editor.js') } | ConvertTo-Json -Depth 4) + "`n")
    return $src
}

# --- 1. 正常构建：allowlist、排除、改写 -------------------------------
$case1 = New-TempCase
$src1 = New-FixtureSource $case1
$out1 = Join-Path $case1 "out"
$webuiSourceBefore = Get-Sha256Hex (Join-Path $src1 "maa-webui.py")
$r = Invoke-Build -Arguments @("-Source", $src1, "-Out", $out1, "-Version", "1.2.3")
Assert-That ($r.ExitCode -eq 0) "正常构建应成功，实际退出码 $($r.ExitCode)：$($r.Output)"
Assert-That (Test-Path -LiteralPath (Join-Path $out1 "project\maa-run.sh")) "构建产物缺少 maa-run.sh"
Assert-That (Test-Path -LiteralPath (Join-Path $out1 "project\arknights-apk-update.sh")) "构建产物缺少 arknights-apk-update.sh"
Assert-That (Test-Path -LiteralPath (Join-Path $out1 "VERSION")) "构建产物缺少 VERSION"
Assert-That (Test-Path -LiteralPath (Join-Path $out1 "inventory.json")) "构建产物缺少 inventory.json"
Assert-That (Test-Path -LiteralPath (Join-Path $out1 "project.sha256")) "构建产物缺少 project.sha256"
Assert-That (-not (Test-Path -LiteralPath (Join-Path $out1 "project\start_webui.sh"))) "start_webui.sh 不得被打包"
Assert-That (-not (Test-Path -LiteralPath (Join-Path $out1 "project\maa-scheduled-reboot.sh"))) "设备改动的 maa-scheduled-reboot.sh 不得被打包"
Assert-That (-not (Test-Path -LiteralPath (Join-Path $out1 "project\maa-edit.sh"))) "设备改动的 maa-edit.sh 不得被打包"
Assert-That (-not (Test-Path -LiteralPath (Join-Path $out1 "project\README.md"))) "非白名单扩展名不得被打包"
Assert-That (-not (Test-Path -LiteralPath (Join-Path $out1 "project\maa-run.sh.bak-20260101"))) "备份文件不得被打包"
Assert-That (-not (Test-Path -LiteralPath (Join-Path $out1 "project\__pycache__"))) "__pycache__ 不得被打包"
Assert-That ((Get-ChildItem -LiteralPath (Join-Path $out1 "project") -Directory).Count -eq 0) "project 目录必须是平的"

$webuiOut = Read-Text (Join-Path $out1 "project\maa-webui.py")
Assert-That ($webuiOut.Contains("PORT = 8089")) "maa-webui.py 端口应改写为 8089"
Assert-That ($webuiOut.Contains($webuiLoopbackBind)) "maa-webui.py 应只绑定回环地址"
Assert-That (-not $webuiOut.Contains("0.0.0.0")) "改写后的 maa-webui.py 不应残留 0.0.0.0"
Assert-That ($webuiOut.Contains("http://127.0.0.1:{PORT}")) "启动日志地址应一并改写"
Assert-That ((Get-Sha256Hex (Join-Path $src1 "maa-webui.py")) -eq $webuiSourceBefore) "构建不得修改权威源文件"

# maa-run.sh 保持只读、未适配的原始字节（preview-only：个人设备路径未参数化）。
Assert-That ((Get-Sha256Hex (Join-Path $out1 "project\maa-run.sh")) -eq (Get-Sha256Hex (Join-Path $src1 "maa-run.sh"))) "maa-run.sh 必须逐字节原样发布"

$inventory = Read-Text (Join-Path $out1 "inventory.json")
Assert-That ($inventory.Contains('"sourceSha256"')) "inventory 必须记录改写前的源校验和"
Assert-That ($inventory.Contains('"name": "maa-webui.py"')) "inventory 应列出 maa-webui.py"
Assert-That ($inventory.Contains('"reason": "device-helper"')) "inventory 应记录被排除的设备脚本"
Assert-That ($inventory.Contains('"reason": "device-mutation"')) "inventory 应记录被排除的设备改动脚本"
foreach ($mutation in @("maa-scheduled-reboot.sh", "maa-edit.sh")) {
    Assert-That ($inventory.Contains('"name": "' + $mutation + '", "reason": "device-mutation"')) "inventory 应以 device-mutation 记录 $mutation"
}
Assert-That ($inventory.Contains('"name": "_legacy_flat", "reason": "directory"') -or $inventory.Contains('"name": "_legacy_flat"')) "inventory 应记录被排除的 _legacy_flat 目录"
Assert-That ($inventory.Contains('"webui-port-8089"')) "inventory 应记录端口改写"
Assert-That ($inventory.Contains('"webui-bind-loopback"')) "inventory 应记录绑定改写"
Assert-That (-not $inventory.Contains('"file": "maa-run.sh"')) "maa-run.sh 不应出现在改写记录里"
Assert-That ($inventory.Contains('"webuiListen": "127.0.0.1:8089"')) "inventory 应声明 manager 上游契约"
Assert-That ($inventory.Contains('"taskLock": "/tmp/maa-task.lock"')) "inventory 应声明任务锁路径"
Assert-That (-not $inventory.Contains("`r")) "inventory.json 必须为 LF"

# 跨主机可重复性：inventory 的文件顺序必须是序号排序，而不是区域设置排序。
$inventoryObject = $inventory | ConvertFrom-Json
$inventoryNames = @($inventoryObject.files | ForEach-Object { $_.name })
$ordinalNames = @($inventoryNames)
[Array]::Sort($ordinalNames, [System.StringComparer]::Ordinal)
Assert-That (($inventoryNames -join "|") -eq ($ordinalNames -join "|")) "inventory 文件顺序必须按 Ordinal 排序（避免 NLS/ICU 差异）"
Assert-That ($inventoryObject.contract.managerPublicPort -eq 8088) "inventory 应声明 manager 对外端口 8088"
Assert-That ($inventoryObject.contract.webuiListen -eq "127.0.0.1:8089") "inventory 应声明 WebUI 回环上游"

# --- 2. 确定性 --------------------------------------------------------
$out1b = Join-Path $case1 "out-b"
$r = Invoke-Build -Arguments @("-Source", $src1, "-Out", $out1b, "-Version", "1.2.3")
Assert-That ($r.ExitCode -eq 0) "第二次构建应成功：$($r.Output)"
Assert-That ((Read-Text (Join-Path $out1 "inventory.json")) -eq (Read-Text (Join-Path $out1b "inventory.json"))) "两次构建的 inventory.json 必须一致"
Assert-That ((Read-Text (Join-Path $out1 "project.sha256")) -eq (Read-Text (Join-Path $out1b "project.sha256"))) "两次构建的 project.sha256 必须一致"

# --- 3. CRLF 归一化与孤立 CR ------------------------------------------
$case3 = New-TempCase
$src3 = New-FixtureSource $case3
$crlf = ("#!/bin/bash`necho a`necho b`n").Replace("`n", "`r`n")
[System.IO.File]::WriteAllText((Join-Path $src3 "maa-crlf-helper.sh"), $crlf, $utf8)
$out3 = Join-Path $case3 "out"
$r = Invoke-Build -Arguments @("-Source", $src3, "-Out", $out3)
Assert-That ($r.ExitCode -eq 0) "CRLF 文件应被归一化而不是失败：$($r.Output)"
Assert-That (-not ([System.IO.File]::ReadAllBytes((Join-Path $out3 "project\maa-crlf-helper.sh")) -contains 13)) "打包后的脚本不得含 CR 字节"
Assert-That ((Read-Text (Join-Path $out3 "inventory.json")).Contains('"normalize-lf"')) "inventory 应记录 LF 归一化"

$case3b = New-TempCase
$src3b = New-FixtureSource $case3b
[System.IO.File]::WriteAllBytes((Join-Path $src3b "maa-stray-cr.sh"), [byte[]](0x23, 0x21, 0x0d, 0x0a, 0x61, 0x0d, 0x62))
$r = Invoke-Build -Arguments @("-Source", $src3b, "-Out", (Join-Path $case3b "out"))
Assert-That ($r.ExitCode -ne 0) "孤立 CR 必须让构建失败"
Assert-That ($r.Output.Contains("BUILD_PROJECT_FAIL")) "失败输出应带 BUILD_PROJECT_FAIL 前缀：$($r.Output)"

# --- 4. 凭证必须让构建失败 --------------------------------------------
$case4 = New-TempCase
$src4 = New-FixtureSource $case4
Write-LfFile (Join-Path $src4 "tg.txt") "123456:token`n"
$r = Invoke-Build -Arguments @("-Source", $src4, "-Out", (Join-Path $case4 "out"))
Assert-That ($r.ExitCode -ne 0) "凭证文件必须让构建失败"
Assert-That ($r.Output.Contains("tg.txt")) "失败信息应点名 tg.txt：$($r.Output)"

# --- 5. 锚点不匹配必须失败 --------------------------------------------
$case5 = New-TempCase
$src5 = New-FixtureSource $case5
Write-LfFile (Join-Path $src5 "maa-webui.py") ((@('#!/usr/bin/env python3', 'PORT = 8088', $webuiLoopbackBind) -join "`n") + "`n")
$r = Invoke-Build -Arguments @("-Source", $src5, "-Out", (Join-Path $case5 "out"))
Assert-That ($r.ExitCode -ne 0) "缺少 0.0.0.0 绑定锚点必须让构建失败"
Assert-That ($r.Output.Contains("anchor") -or $r.Output.Contains("锚点")) "失败信息应说明锚点不匹配：$($r.Output)"

$case5b = New-TempCase
$src5b = New-FixtureSource $case5b
Write-LfFile (Join-Path $src5b "maa-webui.py") ((@('#!/usr/bin/env python3', 'PORT = 8089', $webuiPublicBind) -join "`n") + "`n")
$r = Invoke-Build -Arguments @("-Source", $src5b, "-Out", (Join-Path $case5b "out"))
Assert-That ($r.ExitCode -ne 0) "端口锚点不匹配必须让构建失败"

$case5c = New-TempCase
$src5c = New-FixtureSource $case5c
Write-LfFile (Join-Path $src5c "maa-webui.py") ((Read-Text (Join-Path $src5c "maa-webui.py")) + "PORT = 8088`n")
$r = Invoke-Build -Arguments @("-Source", $src5c, "-Out", (Join-Path $case5c "out"))
Assert-That ($r.ExitCode -ne 0) "锚点出现多次必须让构建失败"

# --- 6. 缺少必需文件 --------------------------------------------------
$case6 = New-TempCase
$src6 = New-FixtureSource $case6
Remove-Item -LiteralPath (Join-Path $src6 "maa-webui.py") -Force
$r = Invoke-Build -Arguments @("-Source", $src6, "-Out", (Join-Path $case6 "out"))
Assert-That ($r.ExitCode -ne 0) "缺少 maa-webui.py 必须让构建失败"

# --- 7. 输出目录安全 --------------------------------------------------
$case7 = New-TempCase
$src7 = New-FixtureSource $case7
$r = Invoke-Build -Arguments @("-Source", $src7, "-Out", $src7)
Assert-That ($r.ExitCode -ne 0) "输出目录等于源目录必须被拒绝"
$r = Invoke-Build -Arguments @("-Source", $src7, "-Out", $case7)
Assert-That ($r.ExitCode -ne 0) "输出目录是源目录父目录必须被拒绝"
$r = Invoke-Build -Arguments @("-Source", $src7, "-Out", (Join-Path $case7 "out"))
Assert-That ($r.ExitCode -eq 0) "正常输出目录应可用：$($r.Output)"

# --- 8. -Check 只校验不写盘 -------------------------------------------
$case8 = New-TempCase
$src8 = New-FixtureSource $case8
$out8 = Join-Path $case8 "out"
$r = Invoke-Build -Arguments @("-Source", $src8, "-Out", $out8, "-Check")
Assert-That ($r.ExitCode -eq 0) "-Check 应成功：$($r.Output)"
Assert-That (-not (Test-Path -LiteralPath $out8)) "-Check 不应写任何产物"
Assert-That ($r.Output.Contains("BUILD_PROJECT_CHECK_OK")) "-Check 应输出 CHECK_OK：$($r.Output)"

# --- 9. 内置脚本源（只读） --------------------------------------------
$realSource = Join-Path $RepoRoot "packaging\project\source"
$case9 = New-TempCase
$out9 = Join-Path $case9 "out"
$webuiSource = Join-Path $realSource "maa-webui.py"
$realWebuiBefore = Get-Sha256Hex $webuiSource
$r = Invoke-Build -Arguments @("-Source", $realSource, "-Out", $out9, "-Version", "0.0.0-dev")
Assert-That ($r.ExitCode -eq 0) "内置脚本源应能构建：$($r.Output)"
if ($r.ExitCode -eq 0) {
    foreach ($required in @("arknights-apk-update.sh", "maa-run.sh", "maa-auto-update.sh", "maa-webui.py", "maa-task-guard.py", "maa_weekly_plan.py", "infrast-operator-requirements.json")) {
        Assert-That (Test-Path -LiteralPath (Join-Path $out9 "project\$required")) "内置源构建缺少 $required"
    }
    foreach ($forbidden in @("start_webui.sh", "ssh_watchdog.sh", "ssh_watchdog_worker.sh", "tg.txt", "maa-run.sh.bak-20260101", "README.md")) {
        Assert-That (-not (Test-Path -LiteralPath (Join-Path $out9 "project\$forbidden"))) "内置源构建不应包含 $forbidden"
    }
    foreach ($mutation in @("maa-scheduled-reboot.sh", "maa-edit.sh")) {
        Assert-That (-not (Test-Path -LiteralPath (Join-Path $out9 "project\$mutation"))) "内置源构建不应包含设备改动脚本 $mutation"
    }
    $realInventoryText = Read-Text (Join-Path $out9 "inventory.json")
    Assert-That ($realInventoryText.Contains('"name": "README.md", "reason": "extension"')) "内置源 inventory 应以 extension 记录被排除的 README.md"
    Assert-That ((Get-Sha256Hex (Join-Path $out9 "project\maa-run.sh")) -eq (Get-Sha256Hex (Join-Path $realSource "maa-run.sh"))) "内置 maa-run.sh 必须原样发布"
    $packedWebui = Read-Text (Join-Path $out9 "project\maa-webui.py")
    Assert-That ($packedWebui.Contains("PORT = 8089")) "内置 maa-webui.py 应改写为 8089"
    Assert-That (-not $packedWebui.Contains("0.0.0.0")) "内置 maa-webui.py 改写后不应含 0.0.0.0"
    Assert-That ((Read-Text (Join-Path $out9 "inventory.json")).Contains('"sourceSha256"')) "内置源 inventory 应记录源校验和"
    Assert-That ((Get-Sha256Hex $webuiSource) -eq $realWebuiBefore) "构建不得改动内置脚本源文件"
}

# --- 10. 与 Go 运行时契约保持一致 -------------------------------------
$goProject = Join-Path $RepoRoot "manager\internal\runtime\project.go"
$goRuntime = Join-Path $RepoRoot "manager\internal\runtime\runtime.go"
Assert-That ((Test-Path -LiteralPath $goProject) -and (Test-Path -LiteralPath $goRuntime)) "找不到 runtime 包源码，无法做跨层契约检查"
if ((Test-Path -LiteralPath $goProject) -and (Test-Path -LiteralPath $goRuntime)) {
    $goText = (Read-Text $goProject) + (Read-Text $goRuntime)
    Assert-That ($goText.Contains($webuiLoopbackBind)) "runtime 契约缺少回环绑定锚点"
    Assert-That ($goText.Contains('PORT = 8089')) "runtime 契约缺少 8089 端口锚点"
    Assert-That ($goText.Contains('0.0.0.0')) "runtime 契约缺少公网绑定禁用检查"
    Assert-That ($goText.Contains('/tmp/maa-task.lock')) "runtime 契约缺少任务锁路径"
}

if ($failures.Count -gt 0) {
    Write-Host "PACKAGING_PROJECT_FAIL checks=$checks failures=$($failures.Count)"
    foreach ($failure in $failures) { Write-Host " - $failure" }
    exit 1
}

Write-Host "PACKAGING_PROJECT_OK checks=$checks"
exit 0
