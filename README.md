# prism-local

An Overleaf-style LaTeX studio that runs on your own computer: an editor, a PDF preview that
stays in sync with the source, an AI agent that edits your project, and a Home page for all
your papers, each kept in git and on GitHub.

- **Editor**: tabs, LaTeX highlighting, search, folding of sections and environments, and
  completion of `\cref{…}`, `\cite{…}` and your own `\newcommand`s. Edits save themselves.
- **Compile** (⌘↵ / Ctrl-Enter): the engine (pdflatex, XeLaTeX, LuaLaTeX) is picked from the
  document, bibtex/biber and makeindex run when needed, and errors are listed with their
  source lines.
- **PDF preview with SyncTeX**: double-click the PDF to jump to the source, ⌘J / Ctrl-J to jump
  back. Links, search and bookmarks work; the PDF can pop out into its own tab.
- **✦ Agent panel**: Claude Code, Codex, DeepSeek's Deep Code, or an API model (DeepSeek,
  OpenAI, Qwen, Kimi, a local Ollama …) edits the project. Every turn ends with a diff and **Undo this turn**.
- **Home page**: every project with a PDF thumbnail, organized in folders (a research topic
  and its papers) and tags.
- **History and GitHub**: changes are committed and pushed by themselves; the History tab shows
  and restores every version of a file.

It is one Python program using only the standard library, listening on `127.0.0.1`. Nothing
to `pip install`, no npm, and it works offline.

## Install

### 1. Prerequisites

Python 3.9 or newer, a TeX distribution and git. The GitHub CLI is optional (for GitHub).

| System | One command |
|---|---|
| Windows | `winget install Python.Python.3.12 MiKTeX.MiKTeX Git.Git GitHub.cli` |
| macOS | `brew install python git gh` and `brew install --cask mactex-no-gui` |
| Debian, Ubuntu | `sudo apt install python3 git gh texlive-latex-extra texlive-xetex texlive-luatex texlive-bibtex-extra biber` |
| Fedora | `sudo dnf install python3 git gh texlive-scheme-medium biber` |
| Arch | `sudo pacman -S python git github-cli texlive-basic texlive-latexextra texlive-xetex texlive-luatex texlive-bibtexextra biber` |

- Chinese documents on Linux also need `texlive-lang-chinese` (Debian, Ubuntu) or
  `texlive-langchinese` (Arch). TeX Live from [tug.org](https://tug.org/texlive/) or TinyTeX
  work as well; without any TeX, [Tectonic](https://tectonic-typesetting.github.io/) on `PATH`
  is used.
- Open a new terminal afterwards, so that the new programs are on `PATH`.

### 2. Get prism-local

```sh
git clone https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex.git prism-local
```

That is the whole installation: there is nothing to build.

### 3. Start it

**Windows**: make a **Prism** shortcut in the Start menu and on the desktop, then click it:

```powershell
powershell -ExecutionPolicy Bypass -File prism-local\launcher\make-shortcut.ps1
```

**Linux**: make a **Prism** menu entry, then start it from your desktop's menu:

```sh
python3 prism-local/launcher/make_desktop_entry.py
```

**macOS**: add **prism-local** to Applications, then open it from Spotlight:

```sh
python3 prism-local/launcher/make_macos_app.py
```

Press **Command-Space**, search for **prism-local**, and press Return. If `/Applications`
is not writable, use `--applications ~/Applications`. The app opens the Home page in your
default browser. Keep the repository and Python installed; see [macOS](#macos) for updates.

**Any terminal**:

```sh
prism-local/bin/prism-home                       # the Home page: http://127.0.0.1:8790/
prism-local/bin/prism-local path/to/paper        # one project's editor
```

The Home page opens. Create a project (**+ New project**) or add a folder you already have
(**Add existing…**); `prism-local/examples/minimal` is a small paper to try it on.

### Optional: GitHub and the AI agent

- **GitHub**: log the GitHub CLI in once, `gh auth login`. Projects can then get a private
  repository each (or one per folder), and every change is pushed by itself.
- **Claude Code** for the agent panel: install it
  (`irm https://claude.ai/install.ps1 | iex` on Windows,
  `curl -fsSL https://claude.ai/install.sh | bash` elsewhere) and run `claude` once to log in.
  [Codex](https://github.com/openai/codex), DeepSeek's
  [Deep Code](https://github.com/lessweb/deepcode-cli) (`npm install -g @vegamo/deepcode-cli`)
  or an API key work too; see
  [Choosing the AI](#choosing-the-ai-claude-code-codex-deepseek-and-other-apis).

### Update

prism-local updates itself: before a server starts (an editor or the Home page), it takes
what is new on GitHub, at most once every five minutes. It only fast-forwards, and leaves the
checkout alone when that could touch work of yours: uncommitted changes, commits GitHub does
not have, a merge or rebase in progress, or a branch that tracks none on GitHub. The server
log says what it did. `PRISM_AUTO_UPDATE=0` switches it off. By hand:

```sh
cd prism-local && git pull
```

Open editors then show **Update: restart**: one click and they run the new version, with your
files saved first. The Home page picks it up the next time it starts.

## Home page

The ⌂ button in an editor opens it. Every project you open, by any route, is listed.

**Projects**

- Home works like a file explorer: folders on the left, what the current folder holds in the
  middle (its subfolders, then its projects), and a preview pane on the right with the
  selected project's first PDF page, `\title`, git state, path and tags. Go back, forward and
  up with the arrows (or Alt+←/→/↑, the mouse's side buttons); click a part of the address
  bar to jump there.
- Two views: **Details**, a table with *Date modified*, *Last opened*, git *Status* and tags
  (click a column heading to sort by it, again to reverse), and **Large icons**, the first
  page of each PDF. Folders come first; a folder's dates are those of the projects in it.
- Click to select, Ctrl/Shift+click or Ctrl+A for several; double-click or Enter opens (a
  folder in the list, a project in its editor). F2 renames, Delete removes, the arrow keys
  move. Search with `/`: it looks through the current folder and every folder below it.
- **Open** starts the project's editor in the background, on a port of its own, and opens it
  in a tab; a second click brings that tab back.
- **+ New project** (or `n`) creates a folder from a template (math paper with amsart, article,
  or empty), optionally with a git and a private GitHub repository. **Add existing…** adds a
  folder you already have.
- A right-click (or the **⋯** in the preview pane) pins, renames, moves to a folder, tags, shows in
  Explorer/Finder, copies the path, or removes from the list (the files are never touched).
- **Rename** changes the project's folder on disk too (also from the editor: click the name at
  the top left). Characters no folder may have (`: * ? " < > | / \`) become `-` in the
  folder's name; the list keeps the name as typed.

**Folders and tags**

- **Folders** in the sidebar group projects: one per research topic, say, with a subfolder for
  each paper, and notes shown at the top. Drag projects and folders (the whole selection)
  onto a folder in the list, the sidebar or the address bar to move them there, or use
  **Move to…**. Deleting a folder moves its contents up a level.
- **Pinned**, **Not in a folder**, a tag and a search list projects flat, with a *Location*
  column saying which folder each is in.
- **Tags** (draft, submitted, a coauthor …): click one to see every project with it; rename it
  or change its color from its ⋯ menu.
- Folders and tags exist only in this list. No file moves on disk unless you choose one
  repository for a folder (below).

**GitHub**

- **One project**: a project without a repository has a **+ GitHub** chip (also in its ⋯ menu,
  and in the editor's GitHub button, which then says *Not on GitHub*). It runs `git init` if
  needed, writes a `.gitignore` (build output, LaTeX's auxiliary files, and `conversations/`
  with saved AI chats unless you untick it), commits everything except files over 50 MB,
  creates a private repository with the GitHub CLI and pushes. An editor that is open finds
  the new repository at once.
- **A whole folder** (its ⋯ menu, **GitHub sync…**) chooses between:
  - *One repository for the whole folder*: the projects are gathered in one folder on disk
    (subfolders become subdirectories, such as `练习/` and `答案/`) with one git and one GitHub
    repository. Each editor commits only its own project there.
  - *A repository for each project*: missing ones are created, on GitHub too if you like.

  The dialog lists what will happen to each project before anything does. Folders move only
  after you confirm, and only while their editors are closed. Nothing is deleted: histories
  come along both ways, and a `.git` that is replaced is kept as `.git-prism-separate` or
  `.git-prism-shared`.
- Repositories are created with the account `gh` is logged in to; set an organization as
  owner in ⚙ Settings. They are always separate from the prism-local repository.

**⚙ Settings**

- The default location of new projects, and whether they get a git and a GitHub repository.
- **Claude account**: prism-local has no login of its own; the agent panel uses whichever
  account Claude Code is logged in to. *Only allow this account* locks it: before every message
  the editor checks that Claude Code uses exactly that account with its subscription, and
  sends nothing if another account or API billing (`ANTHROPIC_API_KEY`, `apiKeyHelper`,
  Bedrock/Vertex …) would be used. A Claude Code profile folder (`CLAUDE_CONFIG_DIR`) lets the
  editor use another login than `claude` in your terminal.

The list and settings live in `%LOCALAPPDATA%\prism-local` on Windows,
`~/.local/state/prism-local` elsewhere, or `$PRISM_STATE_DIR`.

## History and sync with GitHub

Open the Git menu and **Confirm sync target** for an existing repository before its first
remote sync. The menu shows the repository and branch used by both fetch and push. A changed
remote, branch, push URL or URL rewrite pauses remote sync and keeps your local edits and
commits. Correct the configuration or confirm the new target in that menu. A repository
created by Prism is bound to its known target when it is created.

The automatic sync switch controls autosave commits, uploads and closing sync. The explicit
**Save to GitHub now** and **Get changes from GitHub** actions still work with the switch
off. They use the same target checks.

In a project with a repository (its own, or its folder's shared one):

- your changes are committed two minutes after you stop editing (at the latest ten minutes
  after the first), and every agent turn gets a commit of its own with your request as the
  message;
- every commit is pushed, and changes on GitHub (another computer) are pulled in: right when
  the project opens, then every five minutes. Histories that diverged are reported, never
  merged for you;
- when the Home page starts, it checks GitHub for every project in the background (a fetch:
  no file changes) and marks the projects with new commits *↓ N new on GitHub*; opening the
  project brings them in;
- the GitHub button at the top says where things stand, and has *Save to GitHub now*, *Get
  changes from GitHub* and a switch;
- the **History** tab lists every version of the open file, shows what each changed, and
  restores one; the **Diff** tab shows what is not committed yet, or what the last commit
  changed.

Files changed on disk by other programs (Claude Code in a terminal, `git checkout`, another
editor) reload by themselves, and a save never silently overwrites a newer version on disk.

## Writing together

Several co-authors can work on one repository, each with prism-local (or plain git):

- **Edits at the same time are combined.** When GitHub has commits you do not have (a
  co-author pushed), your changes are committed and merged with theirs, then pushed. Changes
  to different files, or to different parts of a file, combine by themselves. Nothing is ever
  rebased or rewritten: a merge commit keeps both sides.
- **When you both changed the same lines**, syncing does not stop and nobody has to step in:
  the file gets both versions, one after the other, so neither disappears from the paper.
  In TeX files comment lines mark the place and say whose version is whose
  (`% [prism-local] Alice's version (from GitHub):` …); a `.bib` gets both without them. The
  GitHub button then says *Both versions kept (N)*, and its menu jumps to each place: keep
  what you want and delete the `[prism-local]` lines whenever it suits you. A figure both
  changed keeps yours, with theirs saved next to it (`fig.from-Alice.png`); a file one deleted
  while the other changed keeps the changed version. Only if this automatic merge itself
  fails is it called off, everything stays as it was, and the menu offers *Compare with
  GitHub's version* and *I've combined them*.
- **With AI agents at work on both sides.** On one computer, you and the agent never write at
  the same moment: a turn starts by committing your edits, what you type during it is saved
  when it ends, and the turn's changes get a commit of their own. While a turn runs, nothing
  from GitHub is merged into the files (it is fetched, and merged as soon as the turn ends),
  and *Save to GitHub now* waits for the turn too, so a merge never changes a file under an
  agent that is rewriting it. Agents are told to leave `[prism-local]` blocks alone. When a
  clash is large and most of the file (an agent reformatted it all), the file keeps one
  version and the other is saved whole next to it (`main.from-Alice.tex`), instead of the
  paper being doubled.
- **It keeps going by itself**: GitHub is checked every minute, but not while you are typing
  (20 seconds after your last save); changes not yet recorded that are in the way (another
  project of a folder's shared repository) are recorded first; a lock file left by a git that
  crashed is cleared after 10 minutes; a merge left half-done when the editor was killed is
  called off at the next start and redone.
- **No force push from here.** Every repository prism-local syncs gets a `pre-push` hook
  that refuses a push which would drop commits from GitHub or delete a branch there: from the
  editor or a terminal. A repository with a pre-push hook of its own keeps it.
- **Agent commits and pushes are prohibited.** The editor removes inherited GitHub tokens
  and blocks normal git transports with `GIT_ALLOW_PROTOCOL`. A child can override this
  environment; these settings are supplemental guards, not an access boundary. Codex uses
  native filesystem permissions and disables command networking. prism-local handles syncing.
- **A force push from elsewhere is undone.** A co-author's own git (or `--no-verify` past the
  hook) can still rewrite the branch on GitHub. Every prism-local copy keeps the full history,
  and its next sync notices (git's log of where GitHub's branch has been) that commits were
  dropped: it merges them back and pushes, an ordinary push, and says who rewrote it. One
  force push cannot erase the team's work. To remove something from history on purpose (a
  file committed by mistake), set `"restore_rewritten": false` in each copy's
  `.git/prism-local.json` first: that copy then stops syncing, keeping everything, until it
  follows the new history (`git reset --keep @{u}`).
- **GitHub's own protection** (no force push, no branch deletion, for every tool) is switched on
  when prism-local creates a repository, where GitHub offers it: for private repositories that
  needs a paid plan (Pro, Team …); on the free plan the hook and the repair above protect.
- **Same line ends for everyone.** Repositories prism-local creates have a `.gitattributes`
  (`* text=auto`), so a co-author on Windows and one on a Mac do not turn every line into a
  change.
- Opening a project pulls first, and the Home page shows which projects have new commits on
  GitHub.

`tests/test_collab.py` plays these situations through with several clones of one repository
(same and different lines, deletions, force pushes from outside that get repaired, agents
trying to push, random edits by three people with and without force pushes, a folder's shared
repository) and checks that no commit that reached the remote is ever lost.

## Keyboard

| Key | Action |
|---|---|
| ⌘S / Ctrl-S | Save now (edits are saved automatically anyway) |
| ⌘↵ / Ctrl-Enter | Save all and compile |
| ⌘J / Ctrl-J | Show the cursor line in the PDF |
| double-click or Ctrl/⌘-click in PDF | Jump to the source line |
| click a link in PDF / Alt+← | Follow a reference, citation, contents entry or URL / go back |
| ⌘F / Ctrl-F after clicking the PDF | Search the PDF (Enter: next, Shift+Enter: previous, Esc: close) |
| Ctrl-Q | Fold or unfold the section, environment or `\[ … \]` at the cursor |
| Ctrl-K Ctrl-0 / Ctrl-K Ctrl-J | Fold all / unfold all |
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

Each project and provider has its own conversation. The server checks the project before
starting a turn and checks a saved CLI session before resuming it. If an old session's
project cannot be verified, start a new chat. Old saved records are kept.

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
  the agent gets no shell (Bash, PowerShell): it reads and edits with its file tools.
- **The agent compiles** with a `compile` tool (`prism_local/mcp_compile.py`) that runs the
  editor's own build, the same as the Compile button: the PDF reloads, the Problems list
  fills in, and the agent gets the errors with their file:line to fix. The API models
  (DeepSeek and the others) get the same tool.
- To let the agent run some shell commands too, allow them in `permissions.allow` of
  `.claude/settings.json` (for example `"Bash(make:*)"`); it then has the shell, limited to
  those commands. The card at the end of a turn names any step that was refused.
- **Ask** mode (`plan`) is read-only.
- Nothing from the editor is sent unless you @-mention it.
- **Add files** to a message with **+**, by dragging them onto the panel, or by pasting an
  image. They are saved in the project's `prism-uploads/` folder (up to 25 MB each) and the
  message tells the agent where they are; Claude Code also reads PDFs and images.
- **Added files go to GitHub.** Each one is committed on its own in the project's repository
  (your other changes are left as they are) and pushed; its chip says *✓ GitHub*, *local*
  (no repository of its own, no remote, or `prism-uploads/` in `.gitignore`) or *not pushed*
  with git's message. A project inside another repository (like `examples/minimal`) is never
  committed to that one.
- The conversation continues across messages until you press **New chat**.
- After each turn, a card lists the changed files with diffs and offers **Undo this turn**.
  - Undo restores a file byte for byte, and only if nobody edited it since that turn.
  - Undo history lives in server memory: the last 50 turns, lost when the server restarts.
  - Change detection covers regular project files, binary files, hidden and excluded
    directories, file modes and link metadata. It does not follow links outside the project
    or scan `.git`. Undo keeps at most 25 MB per file and 100 MB per snapshot; larger files
    are compared by SHA-256 and marked as unavailable for Undo. Read failures are reported.
    Snapshots detect changes; they do not enforce access permissions.
  - For durable history, use git.
- While a turn runs in Edit mode, what you type is saved when the turn ends, so it never
  becomes part of the agent's diff or its Undo. If the agent changed the same file, you get
  the conflict banner instead.
- **Cost**: each message is a full Claude Code run, billed to your Claude plan or API account
  like any other Claude Code usage.
- **Usage limits**: the bars show the 5-hour and 7-day utilization that Claude Code reports.
  - They update after each message.
  - **↻** refreshes them with a tiny Haiku call (counted toward your plan's limits, not billed).
  - Usage from other sessions shows up at the next update.
- The panel finds the CLI on `PATH`. Set `CLAUDE_BIN=/path/to/claude` to override. Start
  prism-local from the same environment you use for `claude`, including any `CLAUDE_CONFIG_DIR`,
  or pick the profile folder in Home → Settings → Claude account to use another login than the
  terminal (log in once with `CLAUDE_CONFIG_DIR=<folder> claude`).

## Choosing the AI: Claude Code, Codex, DeepSeek and other APIs

The menu at the top of the panel (or `/provider <name>`) picks who runs the agent. Each
provider keeps its own conversation, model and effort, so you can switch back and forth.
Providers that are not set up are greyed out; hover one to see what it needs.

| Provider | Kind | Set up with |
|---|---|---|
| `claude` | Claude Code CLI | `claude` on `PATH`, or `CLAUDE_BIN` |
| `codex` | Codex CLI | `codex` on `PATH`, or `CODEX_BIN`; `codex login` |
| `deepcode` | Deep Code CLI (DeepSeek) | `npm install -g @vegamo/deepcode-cli`; `DEEPSEEK_API_KEY`, or its own `~/.deepcode/settings.json` |
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
an isolated named permissions profile, and `resume` to continue the
conversation. Codex reads your `AGENTS.md`. `/effort` sets `model_reasoning_effort`
(`minimal` … `xhigh`). Edit grants native write permission only to the @-mentioned files,
or the project tree when there are no mentions. Repository metadata remains read-only.
Ask grants no project writes and no compile tool. The adapter requires a CLI supporting
named profiles and `--ignore-user-config`; it checks compatibility and login before a turn.
It supplies only the editor's compile MCP, without loading user MCP configuration.
Codex reads and searches files with its native shell tools and edits with `apply_patch`.
The editor sends these tool instructions on every turn, including resumed conversations.

**Deep Code (DeepSeek's terminal agent).** [Deep Code](https://github.com/lessweb/deepcode-cli)
is the CLI that DeepSeek's documentation lists for agents. Each message runs
`deepcode --exec` in the project, with the message on stdin, and `--resume` continues the
conversation. Its own settings apply (`~/.deepcode/settings.json` and `.deepcode/settings.json`: model, API key,
permissions); prism-local passes `DEEPSEEK_API_KEY` on when Deep Code has no key of its own,
and `/model` (`deepseek-v4-pro`, `deepseek-flash`) and `/effort` (`low`, `high`, `max`).
Deep Code prints only its final reply, so the panel shows the tools it used once the turn
ends. Each turn refreshes native tool instructions. A temporary project settings overlay
registers the editor compile MCP for Edit and denies write/delete/MCP categories for Ask.
The overlay restores the exact original settings bytes after the turn; concurrent edits are
kept and reported. User MCP servers require a separate clean Deep Code profile.
Deep Code category checks are not an OS sandbox and cannot restrict writes to single files.
Scope uses post-turn restoration. The panel states this limit.

**API providers (DeepSeek and others).** There is no agent CLI, so prism-local runs the agent
loop itself over the OpenAI chat-completions API with function calling. The model gets five
tools: `list_files`, `read_file` and `search`, plus `write_file` and `edit_file` in Edit mode,
only for files the turn may change. Edit also has the editor's compile tool. It cannot run shell commands. Your
`CLAUDE.md` / `AGENTS.md` is added to its instructions. The model must support function
calling (DeepSeek's `deepseek-chat` does). The conversation lives in server memory and ends
when the server stops. The card at the end of a turn shows the tokens used.

API attachments default to UTF-8 text. For a vision model, configure its provider with
`"input_types": ["text", "image"]`; images then use chat-completions `image_url` content
with a base64 data URL. The editor rejects unsupported image/PDF inputs before a model
request. API PDF transport is not implemented. Codex receives image paths through its
native `--image` option. Deep Code defaults to text attachments pending model validation.

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
  the first is the default), `default_model` (applied to checks and every runtime), `efforts` (for APIs that take
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

## Details: launchers and command line

### The launcher

The Windows shortcut and the Linux menu entry run `launcher/prism_launcher.pyw`. It opens the
Home page in a Chrome or Edge window of its own; each project you open becomes a tab of that
window. The Home page stops about 10 seconds after you close it; open editors keep running
until they are closed too.
The macOS app uses the same launcher and opens the Home page in the default browser.

A shortcut that opens one project's editor directly, skipping the Home page:

```powershell
powershell -ExecutionPolicy Bypass -File launcher\make-shortcut.ps1 -Project D:\path\to\paper
python3 launcher/make_desktop_entry.py --project ~/papers/my-paper --desktop      # Linux
```

- **Options.** `make-shortcut.ps1` takes `-Name`, `-NoDesktop`, `-Folder` (where to put the
  shortcut) and `-Browser`; `make_desktop_entry.py` takes `--name`, `--browser`, `--desktop`
  and `--remove`. To remove a Windows shortcut, delete its `.lnk` file.
- **Browser modes.** `window` (the default): a Chrome or Edge window with a tab strip, holding
  only Prism. `app`: an app window without tabs or address bar; the pop-out PDF gets its own
  window, which suits a second monitor. `default`: a tab in your default browser. With Firefox
  as the default browser, every mode behaves like `default`.
- **Processes.** The launcher exits as soon as the page is open. What stays is one
  `python.exe` for the Home page and one for each open project. A server stops about 10
  seconds after its last page closes (reloading a page does not stop it); a build or an agent
  turn still running is allowed to finish, for at most 10 minutes.
- **Closed pages** are noticed at once through an event stream each page keeps open; pages
  also send heartbeats, and one silent for 2 minutes counts as closed (after the computer
  sleeps, pages get a fresh 2 minutes). If a server stopped while its page stayed open, the
  page says so; click the shortcut again and it reconnects.
- **Stable port.** Each project always gets the same port, between 8800 and 9799, so the
  browser keeps its open tabs and chat per project. If it is taken, the next free one is used.
- **Logs.** `%LOCALAPPDATA%\prism-local\logs\<project>-<hash>.log` (`~/.local/state/prism-local/logs`
  on Linux), the previous run as `.log.1`. If a server cannot start, a dialog shows the log's end.
- **Environment.** The shortcut runs in your normal user environment: TeX (`pdflatex`, `bibtex`
  …), `git`, `gh` and `claude` must be on your user `PATH`.
- `launcher/make_icon.py` redraws `launcher/prism.ico`.

### macOS

From the repository directory:

```sh
python3 launcher/make_macos_app.py                              # /Applications/prism-local.app
python3 launcher/make_macos_app.py --applications ~/Applications # user Applications folder
```

- The installer uses macOS `osacompile`, `sips`, and `codesign` to create an app with the
  existing Prism icon. It registers the app with Launch Services and imports its Spotlight metadata.
  No additional Python package is needed.
- The app uses the Python that ran the installer and the repository in its current location.
  Keep an external drive connected if the repository is on that drive. Run the installer
  again after moving the repository or replacing Python. Updates from `git pull` are loaded
  when the Home and editor servers restart.
- Running the installer again updates its existing app. It refuses to replace an app with
  a different bundle identifier or a symbolic link.
- The app adds the usual Homebrew, TeX Live and user CLI folders to `PATH`. Provider settings
  and project data remain in the locations listed above.
- To remove the launcher, move `prism-local.app` to the Trash. This keeps the repository and
  your papers. Closing the last Home page stops its server as described above.

### Linux

- The menu entry goes to `~/.local/share/applications`, where GNOME, KDE, Xfce and the others
  find it, and runs the launcher with the Python that created it.
- Programs started from a desktop menu do not read `~/.bashrc`. prism-local still finds TeX
  Live in `/usr/local/texlive`, `/opt/texlive` or `~/texlive`, TinyTeX, and `claude` in
  `~/.local/bin`, `~/.npm-global/bin` or `/usr/local/bin`. Anything else must be on the `PATH`
  set in `~/.profile`.
- `zenity`, or `kdialog` on KDE, gives the Browse… folder dialog and the launcher's error
  messages (`python3-tk` also does for the folder dialog).
- A server stopped with `kill` (SIGTERM) or by a logout cleans up as after its last page.
  Stopping a build or an agent turn also stops the programs they started.

### Command line

```sh
bin/prism-local [project] [options]    # one project (default: the current directory)
bin/prism-home [options]               # the Home page
python launcher/prism_launcher.pyw [project | --home] [--browser window|app|default|none] [--port N]
```

- `--port 8765`: the port (`0`: any free port). The Home page uses 8790.
- `--port-tries N`: if the port is taken, try the next N−1 ports.
- `--no-browser`: do not open a browser page.
- `--exit-when-idle`: exit about 10 seconds after the last page is closed. Without it, stop the
  server with Ctrl-C.
- `--ready-file FILE`: once listening, write `{pid, port, url, root}` as JSON to FILE.

## Layout

```
bin/prism-local            command-line launcher
bin/prism-home             command-line launcher for the Home page
launcher/                  one-click launcher: prism_launcher.pyw, make-shortcut.ps1 (Windows),
                           make_desktop_entry.py (Linux), make_macos_app.py (macOS), icon
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
prism_local/backend_*.py   Claude Code, Codex CLI, Deep Code CLI and OpenAI-compatible API backends
prism_local/mcp_compile.py the agent's compile tool (MCP, stdio): builds through the editor server
prism_local/gitsync.py     commits, pushes and pulls the project's changes (History, sync)
prism_local/selfupdate.py  updates prism-local itself from GitHub before a server starts
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
