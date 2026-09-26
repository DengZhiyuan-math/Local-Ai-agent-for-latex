# prism-local

A local, Overleaf/Prism-style studio for LaTeX projects on your own machine:

- **Editor**: CodeMirror 5 with tabs, LaTeX highlighting and search. It autocompletes
  `\cref{…}`/`\eqref{…}` from your labels, `\cite{…}` from your `.bib` files, and `\…` from your
  own `\newcommand`s.
- **Compile and see errors**: one click (⌘↵) builds the project. The engine (pdflatex,
  XeLaTeX or LuaLaTeX) is chosen from the document, bibtex/biber and makeindex run when needed,
  and the engine reruns until references settle. Errors and warnings (undefined references and
  citations included) are listed with their source lines, and a click jumps to the line.
- **PDF preview with SyncTeX**: the preview reloads after every build and keeps its scroll position.
  Double-click the PDF to jump to the source; ⌘J jumps from the source to the PDF. The PDF can
  **pop out into its own tab** and stays in sync there.
- **✦ Agent panel**: an AI agent that edits your project. Pick who runs it: your local
  [Claude Code](https://docs.anthropic.com/en/docs/claude-code) or
  [Codex](https://github.com/openai/codex) CLI, or an API model with a key: DeepSeek, OpenAI,
  OpenRouter, Qwen, Kimi, or a local Ollama/vLLM. Every turn ends with a per-file diff and
  **Undo this turn**. With Claude Code the panel also shows your remaining 5-hour and 7-day
  usage limits.
- **Home page**: all your projects in one place, with PDF thumbnails, titles, git state and
  which ones are open. Create a project from a template, add an existing folder, pin, rename
  or open any project in one click. The ⌂ button in the editor brings you back.
- **Autosave**: edits are saved a moment after you stop typing, as in Overleaf. There is no
  Save button. Auto-compile (in the Compile menu) builds shortly after that.
- **Works with other tools**: files changed on disk (by Claude Code in a terminal, `git
  checkout`, another editor) reload automatically. A save never silently overwrites a newer
  version on disk, and keeps the file's line endings (CRLF or LF).

It is a single Python process that uses only the standard library, listens on `127.0.0.1`, and
needs nothing from npm. The front-end libraries are vendored, so it also works offline.

## Requirements

- Python ≥ 3.9 (tested with 3.11)
- A TeX distribution: [MiKTeX](https://miktex.org) or [TeX Live](https://tug.org/texlive/).
  prism-local runs its programs directly (pdflatex, xelatex, lualatex, bibtex, biber,
  makeindex), so it needs neither Perl nor latexmk. Without a TeX distribution,
  [Tectonic](https://tectonic-typesetting.github.io/) is used if it is on `PATH`. latexmk, or
  commands of your own, can be chosen in `prism.json`.
- Optional: `git`, for the file status markers and the Diff view.
- Optional, for the agent panel, one of:
  - [Claude Code](https://docs.anthropic.com/en/docs/claude-code), logged in (`claude` works
    in your terminal);
  - [Codex CLI](https://github.com/openai/codex), logged in (`codex` works in your terminal);
  - an API key for DeepSeek or another OpenAI-compatible API, in an environment variable (see
    [Choosing the AI](#choosing-the-ai-claude-code-codex-deepseek-and-other-apis)).

## Quick start

```sh
git clone https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex.git prism-local
cd prism-local
bin/prism-local examples/minimal          # opens http://127.0.0.1:8765/
```

To use it on your own project:

```sh
/path/to/prism-local/bin/prism-local /path/to/your/latex-project
# or, from inside the project:
/path/to/prism-local/bin/prism-local
```

Options:

- `--port 8765`: the port. `0` picks any free port.
- `--port-tries N`: if the port is taken, try the next N−1 ports.
- `--no-browser`: do not open a browser page.
- `--exit-when-idle`: exit about 10 seconds after the last editor or PDF page is closed.
- `--ready-file FILE`: once listening, write `{pid, port, url, root}` as JSON to FILE.

Without `--exit-when-idle`, stop the server with Ctrl-C.

## Home page

The Home page manages all your projects:

```sh
bin/prism-home                            # opens http://127.0.0.1:8790/
```

- **Project cards** show the first PDF page, the `\title`, the folder, when a source file last
  changed, the git branch and number of changed files, and a green **Open** badge while an
  editor runs for the project. Search with `/`, sort by recently opened, recently edited or name.
- **Open** starts prism-local for the project in the background (through the launcher, so it
  gets its stable port and stops after its last page closes) and opens the editor in a tab.
  A second click brings that tab back instead of opening another.
- **+ New project** (or `n`) creates a folder from a template (math paper with amsart and
  theorem environments, plain article, or empty), with `prism.json` and optionally a git
  repository, and opens it.
- **A private GitHub repository per project.** Tick *Create a private GitHub repository* when
  creating a project (or make it the default in ⚙ Settings). The project is committed and
  pushed to a new private repository of its own, never to the prism-local repository. The
  repository name comes from the folder name and can be edited. It is created with the
  [GitHub CLI](https://cli.github.com) (`gh`), which must be logged in (`gh auth login`).
  Settings shows which account it uses; set an owner there to create repositories in an
  organization. If GitHub refuses, the project is still created and the message says why.
- **⚙ Settings** also sets the default location for new projects and whether they get a git
  repository.
- **Only one Claude account.** prism-local has no login of its own: the agent panel runs your
  local `claude`, so it uses whichever account Claude Code is logged in to. Settings shows that
  account (from `claude auth status`), and *Only allow this account* locks it. Before every
  message the editor then checks, afresh, that Claude Code is logged in to exactly that account
  with its claude.ai subscription. If it is another account, or something would switch it to
  API billing or another login (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
  `CLAUDE_CODE_OAUTH_TOKEN`, Bedrock/Vertex, or an `apiKeyHelper` or such `env` in the user or
  project `.claude/settings*.json`), nothing is sent and the panel says why. The agent panel
  always shows the account in use.
- The git chip shows the project's own repository, with a link to it on GitHub. A folder
  inside some other repository (such as `examples/minimal`, inside prism-local's) has none of
  its own, and the chip says so.
- **Add folder…** adds an existing LaTeX folder. **Browse…** opens a native folder dialog (tkinter).
- The **⋯** menu pins a project to the top, renames it in the list, shows it in Explorer/Finder,
  copies its path, or removes it from the list. Removing never touches the files.
- Every project you open with prism-local, by any route, is added to the list automatically.
- The ⌂ button in the editor opens the Home page, starting it if needed.

The list is stored in `projects.json`, and the settings in `settings.json`, in the state directory (`%LOCALAPPDATA%\prism-local` on
Windows, `~/.local/state/prism-local` elsewhere, or `$PRISM_STATE_DIR`).

## One-click launcher (Windows)

`launcher/` creates a **Prism** shortcut in the Start menu and on the desktop:

```powershell
powershell -ExecutionPolicy Bypass -File launcher\make-shortcut.ps1
```

Clicking it opens the Home page in a Chrome or Edge window of its own. From there you manage
your projects, and each project you open becomes a tab of that window with its editor. The
Home page stops about 10 seconds after you close it; open editors keep running until they
are closed too.

To skip the Home page for one project, make a shortcut that opens its editor directly:

```powershell
powershell -ExecutionPolicy Bypass -File launcher\make-shortcut.ps1 -Project D:\path\to\paper
```

This creates **Prism · paper**. Clicking a project shortcut:

1. Opens another page if prism-local already runs for that project.
2. Otherwise starts prism-local in the background, with no console window, and opens the
   editor in a new Chrome or Edge window of its own. Chrome is used when it is your default
   browser. The pop-out PDF opens as a second tab in that window.
3. Stops the server about 10 seconds after you close the last Prism page (editor or pop-out PDF).
   Reloading a page does not stop it.

Details:

- **Processes.** The launcher exits as soon as the page is open. What stays behind is one
  `python.exe` (with its hidden `conhost.exe`) for the Home page and one for each open project.
  Nothing else waits in the background.
- **How a closed page is noticed.** Every page keeps a connection (an event stream) open to its
  server. When you close the page, the tab or the whole browser window, the connection drops and
  the server knows at once, even if the page had no time to say goodbye.
- **Stable port.** Each project always gets the same port, between 8800 and 9799, so the browser
  keeps its open tabs and chat per project. If the port is taken, the next free one is used.
- **Logs.** Server output goes to `%LOCALAPPDATA%\prism-local\logs\<project>-<hash>.log`. The
  previous run is kept as `.log.1`. If the server cannot start, a dialog shows the end of the log.
- **Running work.** A build or a Claude turn that is still running when the last page closes is
  allowed to finish, for at most 10 minutes, before the server exits.
- **Fallback.** Pages also send heartbeats. In the rare case that the event stream cannot be
  opened, a page that stops sending heartbeats counts as closed after 2 minutes.
- **Sleep.** After the computer wakes up, pages without an open stream get a fresh 2 minutes
  to check in.
- **Server gone.** If the server stopped while a page was still open, for example because the
  browser discarded a background tab, the page says so. Click the shortcut again and the page
  reconnects by itself.
- **Environment.** The shortcut runs in your normal user environment, so your TeX distribution
  (`pdflatex`, `bibtex` …), `git` and `claude` must be on your user `PATH`.
- **Browser modes.** `-Browser` picks how the editor opens:
  - `window`, the default: a new Chrome or Edge window with a tab strip, holding only Prism.
  - `app`: an app window without tabs or address bar. The pop-out PDF then gets its own app
    window, which suits a second monitor.
  - `default`: a tab in your default browser's current window. With Firefox as the default
    browser, every mode behaves like this.
- Other options of `make-shortcut.ps1`: `-Name`, `-NoDesktop`, and `-Folder` to put the
  shortcut somewhere else. To remove a shortcut, delete the `.lnk` file.

The launcher can also be run directly, on any platform:

```sh
python launcher/prism_launcher.pyw /path/to/paper [--browser window|app|default|none] [--port N]
python launcher/prism_launcher.pyw --home         # the Home page
```

`launcher/make_icon.py` redraws `launcher/prism.ico`.

## Keyboard

| Key | Action |
|---|---|
| ⌘S / Ctrl-S | Save now (edits are saved automatically anyway) |
| ⌘↵ / Ctrl-Enter | Save all and compile |
| ⌘J / Ctrl-J | Show the cursor line in the PDF |
| double-click in PDF | Jump to the source line |
| ⌘L / Ctrl-L | Ask Claude about the selection |
| ⌘/ / Ctrl-/ | Toggle `%` comments |
| ⌘B / Ctrl-B | Show/hide the file sidebar |
| Ctrl-Space | Completion |

## Building

**Compile** (⌘↵) saves every file and builds the project. The built-in builder:

1. picks the engine: `"engine"` in `prism.json`, else a `% !TEX program = xelatex` line at the
   top of the main file, else the packages the preamble loads (fontspec, unicode-math, xeCJK,
   ctex … need XeLaTeX; luacode, luatexja … LuaLaTeX), else pdflatex;
2. runs bibtex or biber when the citations or the `.bib` files changed, and makeindex for
   indexes, glossaries and nomenclature;
3. runs the engine again until the `.aux`, `.toc`, `.bbl` … files stop changing (at most five
   passes), so references and citations are resolved after one click;
4. if a file left by an interrupted build breaks the first pass, removes what earlier builds
   left and starts again.

An unchanged document builds in one pass. Folders with spaces or Chinese names work. biber and
latexmk, which are Perl programs, reach such folders through an ASCII junction on Windows.

The ▾ menu next to **Compile**:

- **Draft** keeps going after TeX errors, so you still get a PDF while you write. Errors are
  still reported in red.
- **Strict** stops at the first error.
- **Check** only appears if you configure it, for example as a project script that runs extra
  lint checks.
- **Auto-compile** builds shortly after you stop typing.
- **Recompile from scratch** deletes what earlier builds left (`.aux`, `.bbl`, …) and builds
  again. The menu also says what the last build ran.

While a build runs, the **Compile** button stops it. A build that takes longer than 10 minutes
(an endless loop in a macro, say) is stopped, and the Output tab shows where TeX was.

## Configuration: `prism.json` (optional)

Place `prism.json` in the project root. Every key is optional:

```json
{
  "main": "main.tex",
  "outdir": "build",
  "engine": "xelatex",
  "builder": "auto",
  "shell_escape": false,
  "build": {
    "check": ["python", "scripts/check.py", "{main}"]
  },
  "files":   ["main.tex", "chapters/**/*.tex", "*.bib"],
  "exclude": ["drafts/old/**"]
}
```

- `main`: the root document. Default: `main.tex`, otherwise the first top-level `.tex` file
  containing `\documentclass`.
- `outdir`: where the build writes `<main>.pdf`, `.log` and `.synctex.gz`. Default: `build`.
- `engine`: `pdflatex`, `xelatex` or `lualatex`. Default: chosen from the document (see
  [Building](#building)).
- `builder`: `auto`, the built-in builder (Tectonic if no TeX distribution is found);
  `latexmk`; or `tectonic`.
- `shell_escape`: `true` lets the document run programs (`-shell-escape`), which minted, svg
  and gnuplottex need. Set it only for documents you trust.
- `build`: commands of your own for the `draft`, `strict` or `check` mode, each an argv list or
  a shell string. `{main}` and `{outdir}` are substituted. A shell string runs with `bash -c`;
  on Windows that is Git for Windows' bash, never WSL's. Your build must produce SyncTeX data
  (`-synctex=1`) for the PDF ↔ source jumps.
- `files`: globs for the file tree. By default every `.tex/.bib/.md/.sty/.cls/.txt/.tikz` file is
  listed, skipping hidden directories, `outdir` and `node_modules`.
- `exclude`: globs to hide from the file tree.

Changes to `prism.json` apply at once, without restarting. A `prism.json` that cannot be used
is reported in the Compile menu and the build output, and the defaults apply.

## The agent panel

This section describes the panel with Claude Code, the default. The next section covers the
other providers and what differs for them.

Each message runs Claude Code headlessly in your project:

```
claude -p --output-format stream-json --verbose --include-partial-messages \
       --permission-mode <acceptEdits|plan> [--resume <session>] [--model <m>] [--effort <e>] \
       --append-system-prompt <…>
```

**Slash commands.** Type `/` in the message box to see every command and skill, with completion
(↑↓ to choose, Tab or ↵ to take one).

- The panel itself handles the commands that only exist in Claude Code's interactive terminal:
  - `/model [name]` shows or sets the model for the next messages (`/model default` to reset).
  - `/effort [level]` does the same for the effort level (`low` … `max`).
  - `/skills` lists the skills Claude Code can use in this project. Click one to use it.
  - `/provider [name]` shows the providers or switches to one (same as the menu in the panel).
  - `/mode edit|ask`, `/clear` (or `/new`), and `/help`.
- Everything else goes to Claude Code as the first thing in the prompt, where it looks for a
  command: skills such as `/code-review`, built-ins that work headlessly such as `/compact`
  and `/context`, and your project's own commands.
- A skill gets the editor context after the command, so it knows what "this" refers to.
  Built-in commands get none.
- Commands that need Claude Code's terminal (for example `/doctor`) are not offered here.

**@-mentions decide what Claude may change.** Type `@` to pick a project file, or the text
selected in the editor (also: select text and press ⌘L). A selection becomes a mention like
`@sections/intro.tex:12-18`, and its text is sent along.

- **With @-mentions**, Claude may change only the mentioned files. This is enforced, not just
  asked: the turn runs in Claude Code's default permission mode with `Edit`/`Write` allowed
  for those files only, so any other write is refused. For a line range, Claude is asked to
  keep to those lines.
- **Without @-mentions**, Claude may change any file in the project and create new ones.
- The line above the message box shows the scope before you send. The card at the end of a
  turn reports blocked edits, and any change outside the mentioned files (for example made
  by an allowed shell command), which **Undo this turn** reverts.
- Each message states its own scope, so a limit from an earlier message does not carry over.

Consequences:

- Claude reads your project's `CLAUDE.md`, skills and `.claude/settings.json`, exactly as it
  would in a terminal. Put writing conventions or rules for the paper in `CLAUDE.md`.
- **Edit** mode (`acceptEdits`) may change files. Nothing can be approved interactively, so
  shell commands are limited to the `permissions.allow` list in `.claude/settings.json`, and
  anything else is refused. The card at the end of a turn names any refused tools.
- **Ask** mode (`plan`) is read-only.
- Nothing from the editor is sent unless you @-mention it.
- The conversation continues across messages until you press **New chat**.
- After each turn, a card lists the changed files with diffs and offers **Undo this turn**.
  - Undo restores a file byte for byte, and only if nobody edited it since that turn.
  - Undo history lives in server memory: the last 50 turns, lost when the server restarts.
  - For durable history, use git.
- While a turn runs in Edit mode, what you type is saved when the turn ends, so it never
  becomes part of the agent's diff or its Undo. If the agent changed the same file, you get
  the conflict banner instead.
- **Cost**: each message is a full Claude Code run, billed to your Claude plan or API account
  like any other Claude Code usage.
- **Usage limits**: the bars show the 5-hour and 7-day utilization that Claude Code reports.
  - They update after each message.
  - **↻** refreshes them with a tiny Haiku call (about $0.001).
  - Usage from other sessions shows up at the next update.
- The panel finds the CLI on `PATH`. Set `CLAUDE_BIN=/path/to/claude` to override. Start
  prism-local from the same environment you use for `claude`, including any `CLAUDE_CONFIG_DIR`.

## Choosing the AI: Claude Code, Codex, DeepSeek and other APIs

The menu at the top of the panel (or `/provider <name>`) picks who runs the agent. Each
provider keeps its own conversation, model and effort, so you can switch back and forth.
Providers that are not set up are greyed out; hover one to see what it needs.

| Provider | Kind | Set up with |
|---|---|---|
| `claude` | Claude Code CLI | `claude` on `PATH`, or `CLAUDE_BIN` |
| `codex` | Codex CLI | `codex` on `PATH`, or `CODEX_BIN`; `codex login` |
| `deepseek` | API | `DEEPSEEK_API_KEY` |
| `openai` | API | `OPENAI_API_KEY` |
| `openrouter` | API | `OPENROUTER_API_KEY` |
| `qwen` | API (DashScope) | `DASHSCOPE_API_KEY` |
| `moonshot` | API (Kimi) | `MOONSHOT_API_KEY` |
| `ollama` | API, local | Ollama running on `127.0.0.1:11434`; no key |

API keys are read from environment variables only, never from a file in your project. On
Windows, set one for your user once and restart prism-local:

```powershell
setx DEEPSEEK_API_KEY "sk-..."
```

**Codex CLI.** Each message runs `codex exec --json` in the project, with the sandbox set to
`workspace-write` in Edit mode and `read-only` in Ask mode, and `resume` to continue the
conversation. Codex reads your `AGENTS.md`. `/effort` sets `model_reasoning_effort`
(`minimal` … `xhigh`). Codex cannot be limited to single files, so with @-mentions prism-local
**undoes any change it made outside the mentioned files** when the turn ends, and says so.

**API providers (DeepSeek and others).** There is no agent CLI, so prism-local runs the agent
loop itself over the OpenAI chat-completions API with function calling. The model gets five
tools: `list_files`, `read_file` and `search`, plus `write_file` and `edit_file` in Edit mode,
only for files the turn may change. It cannot run shell commands or compile. Your
`CLAUDE.md` / `AGENTS.md` is added to its instructions. The model must support function
calling (DeepSeek's `deepseek-chat` does). The conversation lives in server memory and ends
when the server stops. The card at the end of a turn shows the tokens used.

**Your own providers and defaults** go in `~/.prism-local/agents.json` (or the file named by
`PRISM_AGENTS`). Any OpenAI-compatible endpoint works:

```json
{
  "default": "deepseek",
  "providers": {
    "deepseek": { "default_model": "deepseek-reasoner" },
    "codex": { "bin": "C:/Users/me/AppData/Roaming/npm/codex.cmd" },
    "ollama": { "enabled": false },
    "my-vllm": {
      "type": "openai",
      "label": "vLLM on the GPU box",
      "base_url": "http://gpu-box:8000/v1",
      "api_key_env": "MY_VLLM_KEY",
      "models": ["qwen3-32b"]
    }
  }
}
```

- An entry with the name of a built-in provider changes only the fields it gives.
- Fields: `type` (`claude`, `codex` or `openai`), `label`, `bin` (CLIs), `base_url`,
  `api_key_env` (omit it for a server that needs no key), `models` (suggestions for `/model`;
  the first is the default), `default_model`, `efforts` (for APIs that take
  `reasoning_effort`), `headers`, `max_steps` (tool rounds per message, default 40),
  `timeout` (seconds), `enabled`.
- `PRISM_AGENT=<name>` picks the default provider for one run.

**Adding another kind of backend** (another agent CLI, or an API that is not OpenAI-compatible):
subclass `Backend` in `prism_local/backends.py`, or `CliBackend` for a CLI that prints JSON
lines, and register it in `load_backends`. The docstring of `backends.py` lists the events the
panel understands. The agent manager takes care of scopes, diffs and undo for every backend.

## Security model

prism-local is meant for a single user on their own machine.

- It binds to `127.0.0.1` only and rejects requests whose `Host` header is not `127.0.0.1` or `localhost`.
- Requests that another website makes your browser send (an image or script tag, a form, a
  fetch) are refused for everything except the pages themselves, so no other site can start a
  build or the agent, or read your files. Other sites cannot frame the pages either.
- Every state-changing request needs the header `X-Prism-Local: 1`. Browsers send a custom
  header cross-origin only after a CORS preflight, and the server never answers preflights, so
  other websites cannot drive the server.
- The one exception is the goodbye a closing page sends with `navigator.sendBeacon`, which
  cannot set headers. It must come from the same origin, and it only removes a page id that
  has sent a heartbeat. The presence stream is a GET, and it is refused unless it comes from
  the same origin, so other sites cannot keep the server running.
- It reads and writes only text source files inside the project directory. Hidden directories
  and the build directory are excluded.
- Builds allow only TeX's restricted shell escape (a few safe helpers such as makeindex), so a
  document cannot run arbitrary programs, unless `prism.json` sets `"shell_escape": true`.
- Anyone who can reach the port can run your build commands and the agent. Do not expose
  the port to a network: no port forwarding, no `0.0.0.0`.
- With an API provider, the files the model reads and your messages are sent to that
  provider's `base_url`. The key stays in the server's environment and never reaches the page.

## Layout

```
bin/prism-local            command-line launcher
bin/prism-home             command-line launcher for the Home page
launcher/                  one-click launcher: prism_launcher.pyw, make-shortcut.ps1, icon
prism_local/server.py      HTTP server: files, SyncTeX, idle exit
prism_local/build.py       builds: engine choice, bibliographies, indexes, reruns, log parsing
prism_local/proc.py        starting and stopping programs (process trees on Windows)
prism_local/httpbase.py    what both servers share: request checks, presence, idle exit
prism_local/fsutil.py      project files: which ones are shown, safe writes, line endings
prism_local/hub.py         Home page server: project list, templates, starting editors
prism_local/registry.py    shared state: project list, running instances, ports
prism_local/presence.py    which pages are open, for --exit-when-idle
prism_local/agent.py       agent turns for every provider: scope, per-turn diffs and undo
prism_local/backends.py    the backend interface, presets and ~/.prism-local/agents.json
prism_local/backend_*.py   Claude Code, Codex CLI and OpenAI-compatible API backends
prism_local/static/        front end (app.js, pdfview.js, viewer.*, home.*, common.js, app.css)
prism_local/static/vendor/ CodeMirror 5.65.18 (MIT), PDF.js 3.11.174 (Apache-2.0)
examples/minimal/          a small amsart project to try it on
tests/                     python -m unittest discover -s tests
```

## Limitations

- SyncTeX lookup is a compact reimplementation, not the `synctex` library. It is accurate to the
  line for ordinary text and math. Inside complex constructs (tables, TikZ, floats) it can land a few lines off.
- The built-in builder does not run xindy or bib2gls (xindy-style glossaries and indexes,
  glossaries-extra's `\GlsXtrLoadResources`). Use `"builder": "latexmk"` with a latexmkrc
  for those.
- No collaborative editing, and no file creation, rename or delete inside the editor. Use your
  file manager, git or Claude for those. (The Home page can create new projects.)

## License

MIT, see [LICENSE](LICENSE). The vendored third-party libraries keep their own licenses: see
`prism_local/static/vendor/LICENSE-*`.

"Prism" in the name refers to the general idea of an AI-assisted LaTeX workspace. This
project is not affiliated with OpenAI's Prism or with Anthropic.
