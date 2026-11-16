# Installation and troubleshooting

[README](../README.md) · [中文快速安装](installation.zh-CN.md) · [User guide](user-guide.md)

Start with the [one-line installer](../README.md#install) unless you want to
choose each component yourself.

## What you need

| Component | Purpose | Required? |
| --- | --- | --- |
| Python 3.9+ | Runs the local server and launcher | Yes; checked by the installer |
| A browser | Displays the Home page and editor | Yes |
| LaTeX or Tectonic | Compiles `.tex` files into PDFs | For compiling; set up on Home |
| git | Local version history and automatic app updates | Recommended; checked by the installer |
| GitHub CLI (`gh`) and a GitHub account | Syncs papers with GitHub | Optional |
| An AI CLI or API provider | Powers the Agent panel | Optional |

There is no Python package installation, virtual environment, npm install or
frontend build needed for Prism itself. Some optional AI CLIs have their own requirements.

## Where the installer puts Prism

| System | Default folder |
| --- | --- |
| Windows | `%LOCALAPPDATA%\Prism\prism-local` |
| macOS | `~/Library/Application Support/Prism/prism-local` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/prism/prism-local` |

The Windows installer tries winget for missing tools and can fall back to the
python.org installer for Python. If git cannot be installed, it can download a
ZIP copy of Prism; that copy does not automatically update through git.
On macOS, follow the developer-tools installation dialog if one appears.
On Linux, the script prints the prerequisite command for your package manager;
run it yourself and then repeat the installer.

To choose a different app folder, set `PRISM_DIR` before running the installer.
Store your papers in a separate folder of your choice.

**Windows PowerShell:**

```powershell
$env:PRISM_DIR = "D:\Programs\prism-local"
irm https://raw.githubusercontent.com/DengZhiyuan-math/Local-Ai-agent-for-latex/main/install.ps1 | iex
```

**macOS / Linux:**

```sh
curl -fsSL https://raw.githubusercontent.com/DengZhiyuan-math/Local-Ai-agent-for-latex/main/install.sh | PRISM_DIR="$HOME/Applications/prism-local" sh
```

Optional installer variables: `PRISM_NO_SHORTCUT=1` skips creating the launcher;
`PRISM_NO_LAUNCH=1` skips opening the Home page at the end. In PowerShell, set these
with `$env:PRISM_NO_SHORTCUT = "1"` and `$env:PRISM_NO_LAUNCH = "1"`.

## Manual installation

### 1. Install Python and git

Install Python 3.9+ and git through your system's package manager or their official
installers. On Windows, keep Python's **tcl/tk** support enabled for folder dialogs.
On Linux, install the matching tkinter package if you want native folder dialogs.

Check from a new terminal:

```sh
python --version
git --version
```

On macOS/Linux, use `python3` if `python` is unavailable. On Windows, the Python
launcher `py -3` also works if installed. Close and reopen the terminal after
installing a program so it receives the updated `PATH`.

### 2. Download Prism

```sh
git clone https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex.git prism-local
cd prism-local
```

Alternatively, use **Code → Download ZIP** on the
[repository page](https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex)
and extract it. Open a terminal in the extracted folder containing `prism_local`
and `launcher`. ZIP copies need to be downloaded again for updates.

### 3. Start the Home page

Run these commands **from the repository folder**; they do not depend on a shell
launcher or a desktop shortcut.

**Windows PowerShell:**

```powershell
python .\prism_local\hub.py
```

**macOS / Linux:**

```sh
python3 prism_local/hub.py
```

The Home page normally opens at `http://127.0.0.1:8790/`. Keep this terminal open
while using Prism; Ctrl+C stops the server. If it does not open a browser, use the
address printed in the terminal. For a busy or Windows-reserved port, use
`python prism_local/hub.py --port 0` (or `python3` on macOS/Linux) and open the
printed address.

To open one paper directly:

```powershell
python .\prism_local\server.py "D:\Papers\my-paper"
```

```sh
python3 prism_local/server.py "$HOME/Papers/my-paper"
```

### 4. Add a desktop launcher (optional)

Still from the repository folder:

| System | Command |
| --- | --- |
| Windows PowerShell | `powershell -ExecutionPolicy Bypass -File .\launcher\make-shortcut.ps1` |
| macOS | `python3 launcher/make_macos_app.py` |
| Linux | `python3 launcher/make_desktop_entry.py` |

If macOS cannot write to `/Applications`, append `--applications ~/Applications`.
Keep Python and the repository in place. If you move the repository or replace
Python, recreate the launcher. See [launcher options](user-guide.md#details-launchers-and-command-line).

### 5. Install LaTeX and compile

On Home, use **Getting started**, or **Settings → Programs**:

- If TeX is detected, use it as it is.
- **Quick start** downloads Tectonic. Its first compile needs internet access to
  fetch the required packages.
- For a conventional distribution, use **Full LaTeX** where offered, or follow
  the page's platform-specific instructions for MiKTeX, MacTeX or TeX Live.

Then follow [Compile your first paper](../README.md#compile-your-first-paper).
For Chinese documents, install the Chinese language packages and fonts your
document needs. On Debian/Ubuntu, the TeX Live Chinese package is
`texlive-lang-chinese`; on Arch it is `texlive-langchinese`. Documents using
`ctex`, `xeCJK` or `fontspec` normally select XeLaTeX automatically; an explicit
`engine` in `prism.json` overrides detection.

## Update

Run the same installer command again, then close and reopen Prism.
For a git checkout, you can also run from its repository folder:

```sh
git pull --ff-only
```

Git-based installations check for updates in the background when a server starts,
at most once every five minutes. An editor offers **Update: restart** when an
update is ready. Uncommitted changes, local-only commits, a merge/rebase in
progress or a branch without an upstream prevent automatic updates. Keep those
changes and resolve them before updating. `PRISM_AUTO_UPDATE=0` disables this check.

ZIP installations do not update through git; download a fresh copy into a new
folder and recreate its launcher. Your papers and settings are stored separately
if you followed the separate-paper-folder recommendation above.

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| The installer cannot download Prism | Check access to GitHub, then retry. Alternatively download the repository ZIP and use the manual steps above. |
| Windows opens the Microsoft Store instead of Python | Install Python, reopen PowerShell, and try `py -3` in place of `python`. The installer also searches normal Python installation folders. |
| Linux installer says Python or git is missing | Run the package-manager command it prints, then repeat the installer. |
| A program was installed but Prism cannot find it | Close and reopen Prism and your terminal; check **Settings → Programs** again. |
| Home does not open | Start it in a terminal using the manual command above and open the printed address. |
| The terminal says the port is unavailable / access is denied when binding | Use `--port 0` to request a free port and open the printed address. Desktop launchers already try alternative ports. |
| The editor opens but no PDF appears | Click **Compile**, check **Problems** and **Output**, and confirm LaTeX is detected under **Settings → Programs**. |
| Tectonic stalls or fails on the first build | Keep the internet connected for package downloads; inspect **Output** for the download error. A full TeX distribution is another option. |
| Chinese text fails to compile | Check the engine, Chinese TeX packages and the document's required fonts. See the LaTeX notes above. |
| No GitHub login or AI provider is configured | Continue editing and compiling locally; these features are optional. |
| App updates do not apply | Check for local changes in the Prism checkout and restart servers after updating. |

Launcher logs live in `%LOCALAPPDATA%\prism-local\logs` on Windows, or
`~/.local/state/prism-local/logs` on macOS/Linux (unless overridden by
`PRISM_STATE_DIR` or `XDG_STATE_HOME`). They are separate from the installed app.
Project lists and settings also live in the state folder; AI provider configuration
lives in `~/.prism-local/agents.json`.
