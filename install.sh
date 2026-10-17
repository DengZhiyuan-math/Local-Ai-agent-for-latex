#!/bin/sh
# Prism one-line install for macOS and Linux:
#
#   curl -fsSL https://raw.githubusercontent.com/DengZhiyuan-math/Local-Ai-agent-for-latex/main/install.sh | sh
#
# 1. Makes sure Python 3.9 or newer and git are there. On macOS both come with Apple's
#    Command Line Developer Tools: if they are missing, this asks macOS to install them and
#    waits until that is done. On Linux it names the one command that installs them.
# 2. Copies Prism into a folder of its own, or updates the copy that is already there:
#      macOS  ~/Library/Application Support/Prism/prism-local
#      Linux  ${XDG_DATA_HOME:-~/.local/share}/prism/prism-local
# 3. Adds Prism to Applications (macOS, launcher/make_macos_app.py) or to the application
#    menu (Linux, launcher/make_desktop_entry.py).
# 4. Opens the Home page. Getting started there sets up LaTeX, GitHub and the AI.
#
# Running it again is safe: it only updates. Settings, as environment variables:
#
#   PRISM_DIR=/some/folder   install there instead
#   PRISM_NO_SHORTCUT=1      leave Applications / the application menu alone
#   PRISM_NO_LAUNCH=1        do not open the Home page at the end
#
# For tests (tests/test_install_sh.py): PRISM_INSTALL_OS (Darwin or Linux) stands in for
# `uname -s`, PRISM_INSTALL_POLL is the seconds between checks while the developer tools
# install (default 5), PRISM_INSTALL_WAIT the most seconds to wait for them (default 1800),
# PRISM_INSTALL_SYSTEM_APPS the system Applications folder (default /Applications).
#
# POSIX sh only: it runs as `curl ... | sh`, so there is no file, no bash, and the script
# itself arrives on standard input. Hence everything happens in main, called on the last
# line (a download cut short runs nothing), and the programs it starts read /dev/null, not
# the rest of the script.

REPO_URL=https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex.git

say() { printf '%s\n' "$*"; }

# A sentence saying what went wrong and what to do, then stop.
fail() {
    printf '%s\n' "$@" >&2
    exit 1
}

has() { command -v "$1" >/dev/null 2>&1; }

python_ok() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 9))' </dev/null >/dev/null 2>&1; }
git_ok() { "$1" --version </dev/null >/dev/null 2>&1; }

# The first NAME on PATH that passes CHECK, printed. While Apple's developer tools are
# missing, /usr/bin/python3 and /usr/bin/git are only stand-ins that open the "install the
# tools" window when run, so /usr/bin is skipped then (a Homebrew or python.org Python
# elsewhere on PATH still counts).
find_tool() {
    _name=$1 _check=$2 _found=
    _ifs=$IFS
    IFS=:
    set -f
    for _d in $PATH; do
        IFS=$_ifs
        _d=${_d:-.}
        if [ "$os" = Darwin ] && [ -z "$dev_tools" ]; then
            case $_d in /usr/bin|/usr/bin/) continue ;; esac
        fi
        if [ -f "$_d/$_name" ] && [ -x "$_d/$_name" ] && "$_check" "$_d/$_name"; then
            _found=$_d/$_name
            break
        fi
    done
    IFS=$_ifs
    set +f
    [ -n "$_found" ] && printf '%s\n' "$_found"
}

find_python_and_git() {
    py=$(find_tool python3 python_ok)
    git=$(find_tool git git_ok)
}

# What is missing, in words.
missing() {
    if [ -z "$py" ] && [ -z "$git" ]; then say "Python 3.9 or newer and git"
    elif [ -z "$py" ]; then say "Python 3.9 or newer"
    else say "git"
    fi
}

# macOS: Python and git, from Apple's Command Line Developer Tools if need be.
# `xcode-select -p` is asked first: it answers without opening any window.
mac_tools() {
    dev_tools=
    if xcode-select -p >/dev/null 2>&1; then dev_tools=1; fi
    find_python_and_git
    [ -n "$py" ] && [ -n "$git" ] && return 0

    if [ -z "$dev_tools" ]; then
        say "Prism needs Apple's Command Line Developer Tools (they include Python and git), and this Mac does not have them yet."
        xcode-select --install </dev/null >/dev/null 2>&1
        say "A window has opened: click Install there and accept the licence."
        say "This takes a few minutes. Prism waits here and carries on by itself when it is done."
        _poll=${PRISM_INSTALL_POLL:-5}
        _wait=${PRISM_INSTALL_WAIT:-1800}       # 30 minutes, as one check every 5 seconds
        _tries=$((_wait / 5))
        until xcode-select -p >/dev/null 2>&1; do
            _tries=$((_tries - 1))
            if [ "$_tries" -lt 0 ]; then
                fail "The developer tools were still not installed after $((_wait / 60)) minutes." \
                     "If the window was closed, run this command again and click Install."
            fi
            sleep "$_poll"
        done
        dev_tools=1
        say "The developer tools are installed."
        find_python_and_git
        _tries=6                # Python and git may take a moment longer than the tools
        while { [ -z "$py" ] || [ -z "$git" ]; } && [ "$_tries" -gt 0 ]; do
            sleep "$_poll"
            _tries=$((_tries - 1))
            find_python_and_git
        done
        [ -n "$py" ] && [ -n "$git" ] && return 0
    fi
    fail "Prism could not find $(missing), although Apple's developer tools are installed." \
         "Install Python from https://www.python.org/downloads/ (version 3.9 or newer), or run" \
         "  xcode-select --install" \
         "to repair the developer tools, then run this command again."
}

# Linux: Python and git come from the system's software manager, which needs the
# administrator password, so this only says which command to run.
linux_tools() {
    find_python_and_git
    [ -n "$py" ] && [ -n "$git" ] && return 0
    if has apt-get; then _cmd="sudo apt install python3 python3-tk git"
    elif has dnf; then _cmd="sudo dnf install python3 python3-tkinter git"
    elif has pacman; then _cmd="sudo pacman -S --needed python tk git"
    elif has zypper; then _cmd="sudo zypper install python3 python3-tk git"
    else
        fail "Prism needs $(missing), which this computer does not have yet." \
             "Install that with your system's software manager, then run this command again."
    fi
    fail "Prism needs $(missing), which this computer does not have yet." \
         "To install that, run this command (it asks for your password):" \
         "  $_cmd" \
         "then run this command again."
}

default_dir() {
    if [ "$os" = Darwin ]; then
        say "$HOME/Library/Application Support/Prism/prism-local"
    else
        say "${XDG_DATA_HOME:-$HOME/.local/share}/prism/prism-local"
    fi
}

# Copy Prism into $dir, or update the copy there. A folder that holds something else is
# left alone.
get_prism() {
    if [ -e "$dir/.git" ] && [ -f "$dir/prism_local/hub.py" ]; then
        say "Updating Prism in $dir ..."
        if GIT_TERMINAL_PROMPT=0 "$git" -C "$dir" pull --ff-only -q </dev/null >/dev/null 2>&1; then
            say "Prism is up to date."
        else
            say "Prism could not be updated just now, so the copy already there is kept." \
                "It updates itself later, when it starts."
        fi
    elif [ -e "$dir" ] && { [ ! -d "$dir" ] || [ -n "$(ls -A "$dir" 2>/dev/null)" ]; }; then
        fail "There is already something else at $dir, so Prism left it alone." \
             "Move it away, or choose another folder by running" \
             "  curl -fsSL https://raw.githubusercontent.com/DengZhiyuan-math/Local-Ai-agent-for-latex/main/install.sh | PRISM_DIR=/path/to/folder sh"
    else
        say "Downloading Prism into $dir ..."
        _parent=$(dirname "$dir")
        mkdir -p "$_parent" 2>/dev/null ||
            fail "Prism could not create the folder $_parent. Choose another one with PRISM_DIR=/path/to/folder."
        if ! _err=$(GIT_TERMINAL_PROMPT=0 "$git" clone -q "$REPO_URL" "$dir" </dev/null 2>&1 >/dev/null); then
            fail "Prism could not be downloaded. Check the internet connection, then run this command again." \
                 "Details: $_err"
        fi
        say "Prism is downloaded."
    fi
}

# Applications (macOS) or the application menu (Linux). Not being able to is no reason to
# stop: the Home page still opens, and the end says how to start Prism.
add_shortcut() {
    if [ "$os" = Darwin ]; then
        _apps=${PRISM_INSTALL_SYSTEM_APPS:-/Applications}
        if [ ! -d "$_apps" ] || [ ! -w "$_apps" ]; then _apps=$HOME/Applications; fi
        say "Adding Prism to $_apps ..."
        if _err=$("$py" "$dir/launcher/make_macos_app.py" --applications "$_apps" </dev/null 2>&1 >/dev/null); then
            app=$_apps/prism-local.app
            say "Prism is in $_apps as prism-local; Spotlight (Command-Space) finds it too."
        else
            trouble="Prism could not be added to $_apps. Details: $_err"
            say "$trouble"
        fi
    else
        say "Adding Prism to the application menu ..."
        if _err=$("$py" "$dir/launcher/make_desktop_entry.py" </dev/null 2>&1 >/dev/null); then
            say "Prism is in the application menu."
        else
            trouble="Prism could not be added to the application menu. Details: $_err"
            say "$trouble"
        fi
    fi
}

# The Home page: the app just made (macOS), else the launcher the menu entries run. It
# starts the Home server on its own, opens the browser, and returns; the server stops by
# itself a while after its last page is closed.
open_home() {
    say "Opening the Home page ..."
    if [ -n "$app" ] && [ -d "$app" ] && open "$app" </dev/null >/dev/null 2>&1; then
        return 0
    fi
    if ! _err=$("$py" "$dir/launcher/prism_launcher.pyw" --home --browser default </dev/null 2>&1 >/dev/null); then
        trouble="The Home page did not open. Details: $_err"
        say "$trouble"
    fi
}

# How to start Prism later, as a sentence.
how_to_start() {
    if [ -n "$app" ]; then
        say "Next time, open prism-local from Applications or Spotlight."
    elif [ "$os" = Linux ] && [ "${PRISM_NO_SHORTCUT:-0}" = 0 ] && [ -z "$trouble" ]; then
        say "Next time, open Prism from the application menu."
    else
        say "To start Prism, run:" "  \"$py\" \"$dir/launcher/prism_launcher.pyw\" --home"
    fi
}

main() {
    os=${PRISM_INSTALL_OS:-$(uname -s 2>/dev/null)}
    case $os in
        Darwin|Linux) ;;
        *) fail "This installer is for macOS and Linux. On Windows, paste this into PowerShell instead:" \
                "  irm https://raw.githubusercontent.com/DengZhiyuan-math/Local-Ai-agent-for-latex/main/install.ps1 | iex" ;;
    esac
    [ -n "$HOME" ] || fail "HOME is not set, so Prism does not know where it should go." \
                           "Set HOME to your home folder, then run this command again."
    app= trouble= dev_tools=

    say "Installing Prism, the LaTeX editor ..."
    if [ "$os" = Darwin ]; then mac_tools; else linux_tools; fi
    say "Python and git are ready."

    dir=${PRISM_DIR:-$(default_dir)}
    case $dir in
        "~") dir=$HOME ;;
        "~/"*) dir=$HOME/${dir#"~/"} ;;
        /*) ;;
        *) dir=$(pwd)/$dir ;;
    esac
    get_prism

    if [ "${PRISM_NO_SHORTCUT:-0}" = 0 ]; then add_shortcut; fi
    if [ "${PRISM_NO_LAUNCH:-0}" = 0 ]; then open_home; fi

    if [ -n "$trouble" ]; then
        printf '%s\n' "Prism is installed in $dir, but not everything worked (see above)." >&2
        how_to_start >&2
        exit 1
    fi
    say "Prism is ready."
    how_to_start
}

main "$@"
