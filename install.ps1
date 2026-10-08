# Run with: powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
[CmdletBinding()]
param([string]$ToolsDirectory = (Join-Path $env:USERPROFILE '.aside\tools'))
$ErrorActionPreference = 'Stop'

# Embedded double quotes are split by Windows PowerShell 5.1 native argument passing.
$probe = 'import sys; assert sys.version_info >= (3, 9); print(sys.executable)'
if (Get-Command py -ErrorAction SilentlyContinue) {
    $python = & py -3 -c $probe
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = & python -c $probe
} else {
    throw 'Install Python 3.9 or newer, then run install.ps1 again.'
}
if ($LASTEXITCODE -ne 0 -or -not $python -or -not (Test-Path -LiteralPath $python)) {
    throw 'A working Python 3.9+ interpreter is required.'
}

New-Item -ItemType Directory -Force -Path $ToolsDirectory | Out-Null
Add-Type -AssemblyName Microsoft.VisualBasic
foreach ($name in @('aside-sync', 'aside-syncd')) {
    $source = Join-Path $PSScriptRoot "bin\$name"
    $destination = Join-Path $ToolsDirectory $name
    $launcher = "$destination.cmd"
    foreach ($old in @($destination, $launcher)) {
        if (Test-Path -LiteralPath $old) {
            [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($old, 'OnlyErrorDialogs', 'SendToRecycleBin')
        }
    }
    Copy-Item -LiteralPath $source -Destination $destination
    # Resolve Python once. No PATH dependency or Windows shebang association.
    $pythonForCmd = $python.Replace('%', '%%')
    # Switch cmd.exe to UTF-8 before the line containing a Unicode Python path.
    $body = "@echo off`r`nchcp 65001 >nul`r`nset PYTHONUTF8=1`r`n`"$pythonForCmd`" -X utf8 `"%~dp0$name`" %*`r`nexit /b %errorlevel%`r`n"
    [IO.File]::WriteAllText($launcher, $body, (New-Object Text.UTF8Encoding($false)))
    Write-Host "Installed $destination"
}
Write-Host "`nRun: & `"$ToolsDirectory\aside-syncd.cmd`" setup"
Write-Host "Then: & `"$ToolsDirectory\aside-syncd.cmd`" install-scheduled-task"
Write-Host 'Use the full paths above, or add this tools directory to your user PATH.'
