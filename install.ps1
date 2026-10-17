<#
.SYNOPSIS
  Installs Prism, the LaTeX studio that runs on your own computer, with one command.

.DESCRIPTION
  Paste this into PowerShell (Start menu > "PowerShell"):

      irm https://raw.githubusercontent.com/DengZhiyuan-math/Local-Ai-agent-for-latex/main/install.ps1 | iex

  It does five things, and says each one as it goes:
    1. Finds Python 3.9 or newer (with tkinter), or installs Python 3.12 for you only
       (winget, or python.org's installer when there is no winget). No administrator rights.
    2. Finds git, or installs it with winget. Without winget it downloads Prism as a ZIP
       instead; that copy does not update itself until git is installed (the Home page's
       Getting started installs git with one click).
    3. Puts Prism in %LOCALAPPDATA%\Prism\prism-local, or updates the copy already there.
    4. Makes a "Prism" shortcut in the Start menu and on the desktop
       (launcher\make-shortcut.ps1).
    5. Opens the Prism Home page, where Getting started sets up LaTeX and GitHub.

  Nothing else on the computer is changed. Running it again updates Prism.

.PARAMETER Dir
  Where to put Prism (default %LOCALAPPDATA%\Prism\prism-local). Piped into iex, set
  $env:PRISM_DIR instead.

.PARAMETER NoShortcut
  Do not make the Start menu and desktop shortcuts ($env:PRISM_NO_SHORTCUT = "1").

.PARAMETER NoLaunch
  Do not open the Home page at the end ($env:PRISM_NO_LAUNCH = "1").

.EXAMPLE
  irm https://raw.githubusercontent.com/DengZhiyuan-math/Local-Ai-agent-for-latex/main/install.ps1 | iex

.EXAMPLE
  $env:PRISM_DIR = "D:\Programs\prism-local"; irm https://raw.githubusercontent.com/DengZhiyuan-math/Local-Ai-agent-for-latex/main/install.ps1 | iex

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1 -Dir D:\Programs\prism-local -NoLaunch
#>
param(
    [string]$Dir,
    [switch]$NoShortcut,
    [switch]$NoLaunch
)

# Everything runs inside this script block, so that piped into iex (which runs in the
# caller's own scope) none of its settings or helpers stay behind in the user's
# PowerShell, and a failure ends the install, not the user's PowerShell window.
& {
param($Dir, $NoShortcut, $NoLaunch, $Invocation)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"     # the progress bar makes downloads many times slower
# Windows PowerShell 5.1 may not offer TLS 1.2 by itself, which GitHub and python.org require.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Repo = "https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex.git"
$RepoPattern = "github\.com[:/]DengZhiyuan-math/Local-Ai-agent-for-latex(\.git)?/?$"
$ZipUrl = "https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex/archive/refs/heads/main.zip"
# The last Python 3.12 with a Windows installer on python.org; used only without winget.
$PythonUrl = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
# For tests: a local ZIP instead of GitHub's, and no PATH read from the registry, so that
# the programs installed on the test machine are not found.
if ($env:PRISM_INSTALL_ZIP_URL) { $ZipUrl = $env:PRISM_INSTALL_ZIP_URL }
$UseRegistry = $env:PRISM_INSTALL_NO_REGISTRY -ne "1"

function Test-Flag([string]$Value) { return [bool]$Value -and $Value -notin @("0", "false", "no") }
if (-not $Dir) { $Dir = $env:PRISM_DIR }
if (-not $Dir) { $Dir = Join-Path $env:LOCALAPPDATA "Prism\prism-local" }
$NoShortcut = $NoShortcut -or (Test-Flag $env:PRISM_NO_SHORTCUT)
$NoLaunch = $NoLaunch -or (Test-Flag $env:PRISM_NO_LAUNCH)

# A failure the user can act on: the message says what to do, Details what went wrong.
function Stop-Install([string]$Message, $Details) {
    $e = New-Object Exception $Message
    $e.Data["Details"] = @($Details | Where-Object { $_ }) -join "`n"
    throw $e
}

function Say([string]$Text) { Write-Host $Text }
function Warn([string]$Text) { Write-Host $Text -ForegroundColor Yellow }

# Runs a program and collects what it printed, without stopping on its error output
# (Windows PowerShell 5.1 turns a program's error output into errors under "Stop").
function Invoke-Program([string]$Exe, [string[]]$Arguments) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & $Exe @Arguments 2>&1 | ForEach-Object { "$_" }
        $code = $LASTEXITCODE
    } catch {
        $out = @("$_"); $code = -1
    } finally {
        $ErrorActionPreference = $saved
    }
    return [pscustomobject]@{ Code = $code; Output = @($out) }
}

function Find-Program([string]$Name) {
    $cmd = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    return $null
}

# A program just installed is on the PATH saved in the registry, not yet on this
# window's PATH: add what is new there, and git's usual folders.
function Update-SessionPath {
    $parts = @($env:Path -split ";")
    if ($UseRegistry) {
        $parts += [Environment]::GetEnvironmentVariable("Path", "Machine") -split ";"
        $parts += [Environment]::GetEnvironmentVariable("Path", "User") -split ";"
    }
    foreach ($base in @($env:ProgramFiles, "$env:LOCALAPPDATA\Programs")) {
        $d = Join-Path "$base" "Git\cmd"
        if ($base -and (Test-Path -LiteralPath $d)) { $parts += $d }
    }
    $seen = @{}
    $keep = foreach ($p in $parts) {
        $p = [Environment]::ExpandEnvironmentVariables($p.Trim())
        if ($p -and -not $seen.ContainsKey($p.ToLowerInvariant())) { $seen[$p.ToLowerInvariant()] = $true; $p }
    }
    $env:Path = @($keep) -join ";"
}

# Downloads a file (a local path is copied: tests use one instead of a web address).
function Save-Download([string]$Url, [string]$Path) {
    if (Test-Path -LiteralPath $Url) { Copy-Item -LiteralPath $Url -Destination $Path; return }
    Invoke-WebRequest -Uri $Url -OutFile $Path -UseBasicParsing
}

# winget's code for an option it does not know: older versions lack --disable-interactivity.
$WingetUnknownOption = -1978335230      # 0x8A150002
function Invoke-Winget([string]$Id, [string[]]$Extra) {
    $winget = Find-Program "winget"
    $a = @("install", "--id", $Id, "-e", "--silent", "--accept-package-agreements",
           "--accept-source-agreements", "--source", "winget") + $Extra
    $r = Invoke-Program $winget ($a + "--disable-interactivity")
    if ($r.Code -eq $WingetUnknownOption) { $r = Invoke-Program $winget $a }
    return $r
}

# ------------------------------------------------------------------ 1. Python

# Asks a Python whether it is new enough and has tkinter (Prism's folder dialogs use it).
# It prints True/False, its version, and where it is; the path comes as hex UTF-8, so a
# user name in any language survives the console's code page.
$PythonProbe = "import sys, tkinter; print(sys.version_info >= (3, 9)); " +
    "print('.'.join(map(str, sys.version_info[:3]))); print(sys.executable.encode('utf-8').hex())"

function Test-Python([string]$Exe, [string[]]$Pre) {
    $r = Invoke-Program $Exe (@($Pre) + @("-c", $PythonProbe))
    $lines = @($r.Output | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($r.Code -ne 0 -or $lines.Count -lt 3 -or $lines[0] -ne "True") { return $null }
    $hex = $lines[2]
    $bytes = [byte[]]@(for ($i = 0; $i -lt $hex.Length - 1; $i += 2) { [Convert]::ToByte($hex.Substring($i, 2), 16) })
    $path = [Text.Encoding]::UTF8.GetString($bytes)
    if (-not $path) { $path = $Exe }
    return [pscustomobject]@{ Exe = $path; Version = $lines[1] }
}

# The Pythons to try, best first: the py launcher, python on PATH (never the Microsoft
# Store's stand-in under WindowsApps, which only opens the Store), the usual install folders.
function Find-Python {
    $py = Find-Program "py"
    if ($py) { $found = Test-Python $py @("-3"); if ($found) { return $found } }
    $onPath = Get-Command python -CommandType Application -All -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notlike "*\WindowsApps\*" }
    foreach ($c in $onPath) { $found = Test-Python $c.Source @(); if ($found) { return $found } }
    $dirs = foreach ($base in @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles")) {
        if ($base -and (Test-Path -LiteralPath $base)) { Get-ChildItem -LiteralPath $base -Directory -Filter "Python3*" }
    }
    # Python313 before Python39: sort by the number after "Python3".
    $dirs = $dirs | Sort-Object { [int](($_.Name -replace "^Python3(\d+).*", '$1') -replace "\D", "0") } -Descending
    foreach ($d in $dirs) {
        $exe = Join-Path $d.FullName "python.exe"
        if (Test-Path -LiteralPath $exe) { $found = Test-Python $exe @(); if ($found) { return $found } }
    }
    return $null
}

function Install-Python {
    $details = $null
    if (Find-Program "winget") {
        Say "Installing Python 3.12 for you only (this takes a minute or two)..."
        $r = Invoke-Winget "Python.Python.3.12" @("--scope", "user")
        Update-SessionPath
        $found = Find-Python
        if ($found) { return $found }
        Warn "winget could not install Python; trying python.org's installer instead."
        $details = $r.Output | Select-Object -Last 5
    }
    # Tests (no registry) must never run a real installer on the test machine.
    if (-not $UseRegistry) { Stop-Install "Python could not be installed (python.org's installer is not run in tests)." $details }
    Say "Downloading Python 3.12 from python.org..."
    $setup = Join-Path ([IO.Path]::GetTempPath()) ("python-setup-" + [guid]::NewGuid().ToString("N") + ".exe")
    try {
        try { Save-Download $PythonUrl $setup } catch {
            Stop-Install ("Python could not be downloaded. Check the internet connection and run " +
                "this command again, or install Python 3 from https://www.python.org/downloads/ first.") @($details, "$_")
        }
        Say "Installing Python 3.12 for you only..."
        # Per-user, with tkinter and the py launcher, and python on PATH; no administrator rights.
        $p = Start-Process -FilePath $setup -Wait -PassThru -ArgumentList ("/quiet InstallAllUsers=0 " +
            "InstallLauncherAllUsers=0 PrependPath=1 Include_tcltk=1 Include_test=0")
        if ($p.ExitCode -notin @(0, 3010)) { $details = @($details, "python.org installer exit code $($p.ExitCode)") }
    } finally {
        Remove-Item -LiteralPath $setup -Force -ErrorAction SilentlyContinue
    }
    Update-SessionPath
    $found = Find-Python
    if ($found) { return $found }
    Stop-Install ("Python could not be installed. Install Python 3 from https://www.python.org/downloads/ " +
        "(keep ""tcl/tk"" ticked), then run this command again.") $details
}

# ------------------------------------------------------------------ 2. git

function Install-Git {
    if (-not (Find-Program "winget")) { return $null }
    Say "Installing git (Windows may ask for your permission)..."
    $r = Invoke-Winget "Git.Git" @()
    Update-SessionPath
    $git = Find-Program "git"
    if (-not $git) { Warn "git could not be installed just now ($(($r.Output | Select-Object -Last 1)))." }
    return $git
}

# ------------------------------------------------------------------ 3. Prism

function Test-OurCheckout([string]$Git, [string]$Path) {
    if ($Git) {
        $url = (Invoke-Program $Git @("-C", $Path, "config", "--get", "remote.origin.url")).Output | Select-Object -First 1
    } else {                                    # no git: read the url lines of .git\config
        $url = $null
        $config = Join-Path $Path ".git\config"
        if (Test-Path -LiteralPath $config -PathType Leaf) {
            $url = (Get-Content -LiteralPath $config) -match "^\s*url\s*=" | ForEach-Object { ($_ -split "=", 2)[1].Trim() }
        }
    }
    return [bool](@($url) -match $RepoPattern)
}

# A copy downloaded as a ZIP (no git): Prism's own files, but no .git folder.
function Test-OurDownload([string]$Path) {
    return (Test-Path -LiteralPath (Join-Path $Path "launcher\prism_launcher.pyw")) -and
           (Test-Path -LiteralPath (Join-Path $Path "prism_local\hub.py"))
}

function Get-PrismZip([string]$Path) {
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("prism-download-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        $zip = Join-Path $tmp "prism.zip"
        try { Save-Download $ZipUrl $zip } catch {
            Stop-Install "Prism could not be downloaded. Check the internet connection and run this command again." "$_"
        }
        Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $tmp "x")
        # GitHub's ZIP holds one folder, Local-Ai-agent-for-latex-main, with Prism inside.
        $top = Get-ChildItem -LiteralPath (Join-Path $tmp "x") -Directory | Select-Object -First 1
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
        Copy-Item -Path (Join-Path $top.FullName "*") -Destination $Path -Recurse -Force
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Install-Prism([string]$Git, [string]$Path) {
    $NoUpdate = "This copy of Prism does not update itself until git is installed; " +
        "Getting started on the Home page installs git with one click."
    $Other = "The folder $Path already has other files in it, so Prism was not put there. Move " +
        "those files away, or choose another folder (`$env:PRISM_DIR = ""D:\some\folder""), and " +
        "run this command again."
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Stop-Install ("$Path is a file, so Prism was not put there. Choose another folder " +
            "(`$env:PRISM_DIR = ""D:\some\folder"") and run this command again.")
    }
    $exists = Test-Path -LiteralPath $Path
    $empty = -not $exists -or -not (Get-ChildItem -LiteralPath $Path -Force | Select-Object -First 1)

    if (-not $empty -and (Test-Path -LiteralPath (Join-Path $Path ".git"))) {
        if (-not (Test-OurCheckout $Git $Path)) { Stop-Install $Other }
        if (-not $Git) { Say "Prism is already in $Path; it is kept as it is."; return }
        Say "Updating Prism in $Path..."
        $r = Invoke-Program $Git @("-C", $Path, "pull", "--ff-only", "--quiet")
        if ($r.Code -eq 0) { Say "Prism is up to date." }
        else { Warn "Prism could not be updated just now; the copy you have is kept. It updates itself later." }
        return
    }
    if (-not $empty -and (Test-OurDownload $Path)) {
        if ($Git) { Say "Prism is already in $Path (a downloaded copy); it is kept as it is."; Say $NoUpdate; return }
        Say "Downloading the newest Prism into $Path..."
        Get-PrismZip $Path
        Say $NoUpdate
        return
    }
    if (-not $empty) { Stop-Install $Other }

    $parent = Split-Path -Parent $Path
    if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    if ($Git) {
        Say "Downloading Prism into $Path..."
        $r = Invoke-Program $Git @("clone", "--quiet", $Repo, $Path)
        if ($r.Code -ne 0) {
            Stop-Install "Prism could not be downloaded. Check the internet connection and run this command again." $r.Output
        }
    } else {
        Say "Downloading Prism into $Path (as a ZIP, because git is not installed)..."
        Get-PrismZip $Path
        Say $NoUpdate
    }
}

# ------------------------------------------------------------------ main

$code = 0
try {
    if ([Environment]::OSVersion.Platform -ne "Win32NT") {
        Stop-Install ("This command is for Windows. On macOS and Linux run: curl -fsSL " +
            "https://raw.githubusercontent.com/DengZhiyuan-math/Local-Ai-agent-for-latex/main/install.sh | sh")
    }
    # A full folder path (relative to this window's folder), on a drive (C:\...) or a share
    # (\\server\...), without the characters Windows does not allow in names.
    $BadName = "Prism cannot be put in ""$Dir"": that is not a folder name Windows accepts. " +
        "Choose another folder and run this command again."
    try { $full = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Dir) } catch { Stop-Install $BadName "$_" }
    if ($full -notmatch '^([A-Za-z]:\\|\\\\[^\\])' -or $full.Substring(2).IndexOfAny('<>:"|?*'.ToCharArray()) -ge 0) {
        Stop-Install $BadName
    }
    $Dir = $full

    Write-Host "Checking Python... " -NoNewline
    $python = Find-Python
    if ($python) { Say "found $($python.Version)" }
    else { Say "not found."; $python = Install-Python; Say "Python $($python.Version) is installed." }

    Write-Host "Checking git... " -NoNewline
    $git = Find-Program "git"
    if ($git) { Say "found" }
    else {
        Say "not found."
        $git = Install-Git
        if ($git) { Say "git is installed." }
    }

    Install-Prism $git $Dir

    # The shortcut and the Home page run launcher\prism_launcher.pyw with pythonw.exe (no console window).
    $pythonw = Join-Path ([IO.Path]::GetDirectoryName($python.Exe)) "pythonw.exe"
    $launcher = Join-Path $Dir "launcher\prism_launcher.pyw"
    $shortcut = $false
    if (-not $NoShortcut) {
        Say "Making the Prism shortcut in the Start menu and on the desktop..."
        # A Windows PowerShell of its own, so the computer's script policy does not block the
        # script (and this works the same from PowerShell 7, from the Store or not).
        $ps = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
        if (-not (Test-Path -LiteralPath $ps)) { $ps = (Get-Process -Id $PID).Path }
        $r = Invoke-Program $ps @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            (Join-Path $Dir "launcher\make-shortcut.ps1"), "-HomePage", "-Python", $python.Exe)
        $shortcut = $r.Code -eq 0
        if (-not $shortcut) { Warn "The shortcut could not be made ($(($r.Output | Select-Object -Last 1)))." }
    }
    if (-not $NoLaunch) {
        if (Test-Path -LiteralPath $pythonw) {
            Say "Opening the Prism Home page..."
            Start-Process -FilePath $pythonw -ArgumentList "`"$launcher`" --home --browser window" -WorkingDirectory $env:USERPROFILE
        } else {
            Warn "The Home page could not be opened: pythonw.exe is missing next to $($python.Exe)."
        }
    }
    if ($shortcut) { Say "Prism is installed. Open it from the Start menu: Prism." }
    else { Say "Prism is installed in $Dir. Open it with: `"$pythonw`" `"$launcher`" --home" }
} catch {
    $code = 1
    Write-Host ""
    if ($_.Exception.Data.Contains("Details")) {         # one of the failures above (Stop-Install)
        Write-Host $_.Exception.Message -ForegroundColor Red
        $details = $_.Exception.Data["Details"]
    } else {                                              # anything else: say what to do all the same
        Write-Host ("Prism was not installed because something unexpected went wrong. Run this " +
            "command again; if it happens again, please report the details below.") -ForegroundColor Red
        $details = "$_`n$($_.InvocationInfo.PositionMessage)"
    }
    if ($details) { Write-Host "Details: $details" -ForegroundColor DarkGray }
}

# A failure ends with a non-zero exit code. Run as a file, that is the script's exit code;
# piped into iex, "exit" would close the user's PowerShell, so it is only $LASTEXITCODE.
if ($code -ne 0) {
    if ($Invocation.MyCommand.CommandType -eq "ExternalScript" -and
        "$($Invocation.MyCommand.ScriptContents)" -like "*PRISM-INSTALL-PS1*") { exit $code }
    $global:LASTEXITCODE = $code
}
} $Dir $NoShortcut $NoLaunch $MyInvocation   # PRISM-INSTALL-PS1 (marks this file; see above)
