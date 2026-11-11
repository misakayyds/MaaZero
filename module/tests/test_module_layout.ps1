#Requires -Version 5.1
<#
.SYNOPSIS
    Read-only checks for the module/ source tree: line endings, required files,
    fixed start command, rescue entry and data-preservation boundaries.
.DESCRIPTION
    This script never builds, installs or connects to a device. It protects the
    "the module is only a rescue layer" invariants: LF text, a stable module id,
    loopback-only listen address, fixed command entrypoints, data kept on
    uninstall and no embedded credentials.
#>
[CmdletBinding()]
param(
    [string]$ModuleRoot = ""
)

$ErrorActionPreference = "Stop"
# Windows PowerShell 5.1 evaluates param defaults before $PSScriptRoot is set
# when the script also declares #Requires/comment-based help, so resolve here.
if ($ModuleRoot.Length -eq 0) {
    $ModuleRoot = Join-Path $PSScriptRoot ".."
}
$ModuleRoot = (Resolve-Path -LiteralPath $ModuleRoot).Path
$checks = 0
$failures = New-Object "System.Collections.Generic.List[string]"

function Assert-That {
    param([bool]$Condition, [string]$Message)
    $script:checks++
    if (-not $Condition) { $script:failures.Add($Message) }
}

function Read-TextFile {
    param([string]$Path)
    [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
}

$requiredFiles = @(
    "module.prop",
    "customize.sh",
    "service.sh",
    "action.sh",
    "uninstall.sh",
    "webroot/index.html"
)

$modulePropPath = Join-Path $ModuleRoot "module.prop"
$servicePath = Join-Path $ModuleRoot "service.sh"
$actionPath = Join-Path $ModuleRoot "action.sh"
$uninstallPath = Join-Path $ModuleRoot "uninstall.sh"
$webrootPath = Join-Path $ModuleRoot "webroot"
$webrootIndexPath = Join-Path $webrootPath "index.html"

foreach ($relative in $requiredFiles) {
    $slashed = $relative -replace "/", [string][System.IO.Path]::DirectorySeparatorChar
    $path = Join-Path $ModuleRoot $slashed
    Assert-That (Test-Path -LiteralPath $path -PathType Leaf) "missing required file: $relative"
}

# The source tree must not carry the ARM64 binary; packaging/build-module.ps1 supplies it.
Assert-That (-not (Test-Path -LiteralPath (Join-Path $ModuleRoot "bin"))) "module/ source tree must not contain bin/; maa-managerd is supplied by the packager"

# LF and BOM checks cover every file under module/, including this test.
$files = @(Get-ChildItem -LiteralPath $ModuleRoot -Recurse -File)
Assert-That ($files.Count -gt 0) "module directory contains no files"
foreach ($file in $files) {
    $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
    $relative = $file.FullName.Substring($ModuleRoot.Length).TrimStart([char]92, [char]47)
    Assert-That ($bytes.Length -gt 0) "$relative is empty"
    Assert-That (-not ($bytes -contains 13)) "$relative contains a CR byte; LF only"
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 239 -and $bytes[1] -eq 187 -and $bytes[2] -eq 191)
    Assert-That (-not $hasBom) "$relative starts with a UTF-8 BOM"
}

if (Test-Path -LiteralPath $modulePropPath -PathType Leaf) {
    $propText = Read-TextFile $modulePropPath
    $props = @{}
    $duplicateKeys = New-Object "System.Collections.Generic.List[string]"
    foreach ($line in ($propText -split "`n")) {
        if ($line.Trim().Length -eq 0) { continue }
        if ($line -match "^\s*#") { continue }
        Assert-That ($line -notmatch "\s+$") "module.prop line has trailing whitespace: $line"
        $pair = $line.Split("=", 2)
        Assert-That ($pair.Count -eq 2) "module.prop line is not key=value: $line"
        if ($pair.Count -eq 2) {
            if ($props.ContainsKey($pair[0])) { $duplicateKeys.Add($pair[0]) }
            $props[$pair[0]] = $pair[1]
        }
    }
    Assert-That ($duplicateKeys.Count -eq 0) ("module.prop has duplicate keys: " + ($duplicateKeys -join ", "))
    Assert-That ($props["id"] -eq "maa-phone-manager") "module.prop id must stay the stable maa-phone-manager"
    Assert-That ($props["versionCode"] -match "^\d+$") "module.prop versionCode must be an integer"
    if ($props["versionCode"] -match "^\d+$") {
        Assert-That ([int]$props["versionCode"] -ge 1) "module.prop versionCode must be >= 1"
    }
    Assert-That ($props["version"] -match "preview") "module.prop version must be marked as preview"
    Assert-That ($props["name"].Trim().Length -gt 0) "module.prop is missing name"
    Assert-That ($props["author"].Trim().Length -gt 0) "module.prop is missing author"
    Assert-That ($props["description"] -match "(?i)preview") "module.prop description must say this is a preview"
} else {
    Assert-That $false "module.prop missing; module metadata cannot be checked"
}

foreach ($script in @("customize.sh", "service.sh", "action.sh", "uninstall.sh")) {
    $path = Join-Path $ModuleRoot $script
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        Assert-That $false "$script missing; shebang cannot be checked"
        continue
    }
    $text = Read-TextFile $path
    Assert-That ($text.StartsWith("#!/system/bin/sh")) "$script must start with #!/system/bin/sh"
    Assert-That ($text -notmatch "curl|wget") "$script must not download anything on the device"
}

if (Test-Path -LiteralPath $servicePath -PathType Leaf) {
    $service = Read-TextFile $servicePath
    Assert-That ($service -match "DATA_ROOT=/data/local/maa-platform") "service.sh must use the fixed data root"
    Assert-That ($service -match "LISTEN=127\.0\.0\.1:8088") "service.sh must pin the loopback listen address"
    Assert-That ($service.IndexOf('--listen "$LISTEN"') -ge 0) "service.sh must pass the pinned listen address to serve"
    Assert-That ($service -notmatch "0\.0\.0\.0") "service.sh must never listen on 0.0.0.0"
    Assert-That ($service -match "serve --data-root") "service.sh must call serve --data-root"
    Assert-That ($service -match "--config") "service.sh must pass the release.json config path"
    Assert-That ($service -match "--assets") "service.sh must pass the webroot/assets path"
    Assert-That ($service -match "nohup") "service.sh must start the daemon in the background"
    Assert-That ($service -match "&(`r)?`n") "service.sh must background the serve invocation"
    Assert-That ($service -match "(?m)^exit 0") "service.sh must end with exit 0"
    Assert-That ($service -match "/proc/\`$[a-z_]+/cmdline") "service.sh must verify the exact cmdline instead of matching a substring"
    Assert-That ($service -match "argv1") "service.sh must also verify the serve argument"
    Assert-That ($service -match "argv1.*=.*serve|serve.*argv1") "service.sh must compare the second argv entry against serve"
    Assert-That ($service -match "(?m)^umask 077") "service.sh must set umask 077 so created directories are root-private"
    Assert-That (($service.IndexOf("umask 077") -ge 0) -and ($service.IndexOf("umask 077") -lt $service.IndexOf("`nmkdir"))) "umask 077 must come before the first mkdir command"
    Assert-That ($service.IndexOf("chmod") -lt 0) "service.sh must not chmod existing directories; main fails closed instead"
    Assert-That ($service.IndexOf('-L "$DATA_ROOT"') -ge 0) "service.sh must refuse a symlinked data root"
    Assert-That ($service.IndexOf('-L "$LOG_DIR"') -ge 0) "service.sh must refuse a symlinked log directory"
    Assert-That ($service.IndexOf('-L "$LOG"') -ge 0) "service.sh must refuse a symlinked log file"
    Assert-That ($service.IndexOf('-L "$PIDFILE"') -ge 0) "service.sh must refuse a symlinked pid file"
    Assert-That ($service.IndexOf("stat -c") -ge 0) "service.sh must verify ownership and mode with stat before writing"
    Assert-That ($service.IndexOf("require_root_700") -ge 0) "service.sh must gate writes on a root:700 check"
    Assert-That (($service.IndexOf("require_root_700") -ge 0) -and ($service.IndexOf("require_root_700") -lt $service.IndexOf('log_line "missing'))) "the root:700 check must run before the first log write"
    Assert-That (($service.IndexOf("require_root_700") -ge 0) -and ($service.IndexOf("require_root_700") -lt $service.IndexOf("nohup"))) "the root:700 check must run before launching the daemon"
    Assert-That (($service.IndexOf("require_root_700") -ge 0) -and ($service.IndexOf("require_root_700") -lt $service.IndexOf('> "$PIDFILE"'))) "the root:700 check must run before writing the pid file"
    Assert-That ($service -match "wait_ready") "service.sh must verify readiness before reporting success"
    Assert-That ($service -match "0100007F:1F90") "readiness must check the fixed loopback port (127.0.0.1:8088)"
    Assert-That ($service.IndexOf('/proc/$target_pid/fd') -ge 0) "readiness must inspect the daemon's own file descriptors"
    Assert-That ($service.IndexOf("socket:") -ge 0) "readiness must resolve the daemon's listening socket inode"
    Assert-That ($service.IndexOf('$inode') -ge 0) "readiness must match the socket inode, not just the global port"
    Assert-That ($service.IndexOf("awk") -ge 0) "readiness must use awk field matching instead of a substring grep"
    Assert-That ($service.IndexOf('$10 "" == want') -ge 0) 'readiness must compare the inode field exactly ($10 == want)'
    Assert-That ($service.IndexOf('$4 == "0A"') -ge 0) "readiness must require the LISTEN state field"
    Assert-That ($service.IndexOf('> /dev/null 2>>') -ge 0) "service.sh must not persist daemon stdout; it may carry one-time session tickets"
    Assert-That (($service.IndexOf("if wait_ready") -ge 0) -and ($service.IndexOf("if wait_ready") -lt $service.IndexOf([char]34 + "started pid"))) "started must only be logged after the readiness check"
    Assert-That ($service -match "failed to start") "service.sh must log a distinct failure instead of claiming it started"
}

if (Test-Path -LiteralPath $actionPath -PathType Leaf) {
    $action = Read-TextFile $actionPath
    Assert-That ($action -match "open-ui --data-root") "action.sh must call the fixed open-ui command"
    Assert-That ($action -notmatch "--listen") "action.sh must not start its own listener"
    Assert-That ($action -notmatch "(?i)token") "action.sh must not write token files"
}

if (Test-Path -LiteralPath $uninstallPath -PathType Leaf) {
    $uninstall = Read-TextFile $uninstallPath
    Assert-That ($uninstall -match "stop --data-root") "uninstall.sh must stop the daemon through the binary"
    Assert-That ($uninstall -notmatch "pkill") "uninstall.sh must not use pkill"
    Assert-That ($uninstall -notmatch "killall") "uninstall.sh must not use killall"
    Assert-That ($uninstall -notmatch "(^|[^\w-])rm(\s|$)") "uninstall.sh must not delete files"
    Assert-That ($uninstall -match "/proc/\`$pid/cmdline") "uninstall.sh fallback stop must verify the exact cmdline"
    Assert-That ($uninstall -match "argv1") "uninstall.sh fallback stop must also verify the serve argument"
    Assert-That ($uninstall -match "DATA_ROOT") "uninstall.sh must name the data root it preserves"
}

$secretPatterns = @(
    "[0-9]{8,10}:[A-Za-z0-9_-]{35}",
    "(?i)private key",
    "(?i)(token|secret|password|passwd|api_key)\s*=",
    "tg\.txt"
)
# This scanner necessarily contains the literal patterns themselves, so it is
# the only file excluded from the credential scan. It is never packaged.
$scannerPath = $PSCommandPath
foreach ($file in $files) {
    if ($file.FullName -eq $scannerPath) { continue }
    $text = Read-TextFile $file.FullName
    $relative = $file.FullName.Substring($ModuleRoot.Length).TrimStart([char]92, [char]47)
    foreach ($pattern in $secretPatterns) {
        Assert-That ($text -notmatch $pattern) "$relative matches a credential-like pattern: $pattern"
    }
}

if (Test-Path -LiteralPath $webrootIndexPath -PathType Leaf) {
    $webroot = Read-TextFile $webrootIndexPath
    Assert-That ($webroot -notmatch "(?i)<input") "webroot must not expose an input field"
    Assert-That ($webroot -notmatch "(?i)<textarea") "webroot must not expose a free-text area"
    Assert-That ($webroot -notmatch "(?i)<form") "webroot must not submit a form"
    Assert-That ($webroot -match "ksu\.exec") "webroot must use the KernelSU/APatch exec interface for the fixed command"
    Assert-That ($webroot -match "open-ui") "webroot may only call open-ui"
    Assert-That ($webroot -match "/data/adb/modules/maa-phone-manager/bin/maa-managerd") "webroot must use the fixed module binary path"
    Assert-That ($webroot -match "127\.0\.0\.1:8088") "webroot must link the fixed local page"
    Assert-That ((Read-TextFile $actionPath) -match "umask 077") "action.sh must also run with umask 077"
    Assert-That ($webroot.IndexOf("window[CALLBACK_NAME]") -ge 0) "webroot must register a named callback for ksu.exec"
    Assert-That ($webroot.IndexOf('ksu.exec(COMMAND, "{}", CALLBACK_NAME)') -ge 0) "webroot must pass the callback name string, as the KernelSU bridge requires"
    Assert-That ($webroot.IndexOf("ksu.exec" + "(COMMAND" ) -ge 0) "webroot must keep calling the fixed command"
    Assert-That ($webroot.IndexOf('ksu.exec(COMMAND, "{}", function') -lt 0) "webroot must not pass a function literal to ksu.exec"
    Assert-That ($webroot -notmatch "(?i)proxy|maa-webui") "webroot must not proxy or claim the existing MAA WebUI"
    Assert-That ($webroot.IndexOf("stdout") -lt 0) "webroot must not render command output; it may carry one-time session tickets"
    Assert-That ($webroot.IndexOf("stderr") -lt 0) "webroot must not render command errors; they may carry one-time session tickets"
    Assert-That ($webroot.IndexOf("preview/recovery entry") -ge 0) "webroot must state that it is the local manager preview/recovery entry"
}

if ($failures.Count -gt 0) {
    Write-Host "MODULE_LAYOUT_FAIL checks=$checks failures=$($failures.Count)"
    foreach ($failure in $failures) { Write-Host " - $failure" }
    exit 1
}

Write-Host "MODULE_LAYOUT_OK checks=$checks files=$($files.Count)"
exit 0
