<#
.SYNOPSIS
  Creates Start menu and desktop shortcuts that open a LaTeX project, or the
  Prism Home page with all your projects, in prism-local.

.DESCRIPTION
  The shortcut runs launcher\prism_launcher.pyw with pythonw.exe, so no console
  window appears. Clicking it starts prism-local for the project (or reuses a
  running one) and opens the editor; closing the last Prism page stops it.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File launcher\make-shortcut.ps1 -Home

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File launcher\make-shortcut.ps1 -Project D:\DynNum

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File launcher\make-shortcut.ps1 -Project D:\DynNum -Name "DynNum paper" -Browser app -NoDesktop

.PARAMETER HomePage
  Create a shortcut named "Prism" for the Home page, which lists your projects and
  opens each one. This is what you get when -Project is left out.

.PARAMETER Browser
  window (default): a new Chrome/Edge window of its own; the pop-out PDF opens as a tab in it.
  app: an app window without tabs or address bar; the pop-out PDF opens in its own window.
  default: a tab in the default browser's current window.

.PARAMETER Python
  The python.exe (or pythonw.exe) the shortcut should use: pythonw.exe in the same folder
  runs the launcher. install.ps1 passes the Python it found or installed. Left out, the
  Python of the py launcher is used, else pythonw.exe on PATH.
#>
param(
    [string]$Project,
    [Alias("Home")][switch]$HomePage,
    [string]$Name,
    [ValidateSet("window", "app", "default")][string]$Browser = "window",
    [switch]$NoDesktop,
    [string]$Folder,         # create the shortcut only in this folder
    [string]$Python          # use the pythonw.exe next to this Python
)
$ErrorActionPreference = "Stop"

$HomePage = $HomePage -or -not $Project
if (-not $HomePage) {
    $Project = (Resolve-Path -LiteralPath $Project).Path
    if ($Project.Length -gt 3) { $Project = $Project.TrimEnd('\') }   # a trailing \ would escape the quote
}
if (-not $Name) {
    $Name = if ($HomePage) { "Prism" } else { "Prism " + [char]0x00B7 + " " + (Split-Path $Project -Leaf) }
}

$launcher = Join-Path $PSScriptRoot "prism_launcher.pyw"
$icon = Join-Path $PSScriptRoot "prism.ico"
if (-not (Test-Path -LiteralPath $launcher)) { throw "Launcher not found: $launcher" }

# pythonw.exe of a real Python install (not the Microsoft Store alias).
$pythonw = $null
if ($Python) {
    $cand = Join-Path ([IO.Path]::GetDirectoryName($Python)) "pythonw.exe"
    if (-not (Test-Path -LiteralPath $cand)) { throw "pythonw.exe not found next to $Python." }
    $pythonw = $cand
}
$py = Get-Command py.exe -ErrorAction SilentlyContinue
if (-not $pythonw -and $py) {
    $exe = & $py.Source -3 -c "import sys; print(sys.executable)" 2>$null
    if ($exe) { $cand = Join-Path (Split-Path $exe) "pythonw.exe"; if (Test-Path $cand) { $pythonw = $cand } }
}
if (-not $pythonw) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notlike "*\WindowsApps\*" } | Select-Object -First 1
    if ($cmd) { $pythonw = $cmd.Source }
}
if (-not $pythonw) { throw "pythonw.exe not found. Install Python 3.9 or newer from python.org." }

if ($Folder) {
    $folders = @((Resolve-Path -LiteralPath $Folder).Path)
} else {
    $folders = @([Environment]::GetFolderPath("Programs"))
    if (-not $NoDesktop) { $folders += [Environment]::GetFolderPath("Desktop") }
}

# WScript.Shell writes shortcuts through the ANSI code page, which turns Chinese (or
# other non-Latin) folder names into "?". So it only creates an empty shortcut under a
# plain temporary name; Shell.Application, which is Unicode-aware, fills in the fields.
$wsh = New-Object -ComObject WScript.Shell
$app = New-Object -ComObject Shell.Application
foreach ($dir in $folders) {
    $path = Join-Path $dir "$Name.lnk"
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("prism-shortcut-" + [guid]::NewGuid().ToString("N") + ".lnk")
    $stub = $wsh.CreateShortcut($tmp)
    $stub.TargetPath = $pythonw
    $stub.Save()
    Move-Item -LiteralPath $tmp -Destination $path -Force
    $link = $app.Namespace($dir).ParseName("$Name.lnk").GetLink
    $link.Path = $pythonw
    if ($HomePage) {
        $link.Arguments = "`"$launcher`" --home --browser $Browser"
        $link.WorkingDirectory = $env:USERPROFILE
        $link.Description = "Open the Prism Home page with your LaTeX projects"
    } else {
        $link.Arguments = "`"$launcher`" `"$Project`" --browser $Browser"
        $link.WorkingDirectory = $Project
        $link.Description = "Open $Project in prism-local"
    }
    if (Test-Path -LiteralPath $icon) { $link.SetIconLocation($icon, 0) }
    $link.Save()
    Write-Host "Created $path"
}
Write-Host "Python:  $pythonw"
Write-Host $(if ($HomePage) { "Home page" } else { "Project: $Project" })
