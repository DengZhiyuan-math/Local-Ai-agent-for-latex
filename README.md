# prism-local

An Overleaf-style LaTeX studio that runs on your own computer: an editor, a PDF preview that
stays in sync with the source, an AI agent that edits your project, and a Home page for all
your papers, each kept in git and on GitHub.

- **Editor**: tabs, LaTeX highlighting, find and replace in a file or the whole project, go to
  line, search with the agent's help, folding of sections and environments, and
  completion of `\cref{…}`, `\cite{…}` and your own `\newcommand`s. Edits save themselves.
- **Files**: drag files onto the sidebar to add them to the project (up to 25 MB each).
  Different files with the same name get a number; existing files are kept. Text files
  open in the editor; images and PDFs open in a new tab; other files download.
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

### One line

**Windows**: open **PowerShell** (Start menu → type PowerShell), paste this and press Enter:

```powershell
irm https://raw.githubusercontent.com/DengZhiyuan-math/Local-Ai-agent-for-latex/main/install.ps1 | iex
```

**macOS and Linux**: open **Terminal**, paste this and press Enter:

```sh
curl -fsSL https://raw.githubusercontent.com/DengZhiyuan-math/Local-Ai-agent-for-latex/main/install.sh | sh
```

It finds or installs Python and git (Windows: for you only, with winget; macOS: Apple's
developer tools, one click in the window that opens; Linux: it names the one command to
run), puts Prism in a folder of its own (`%LOCALAPPDATA%\Prism\prism-local`,
`~/Library/Application Support/Prism/prism-local`, `~/.local/share/prism/prism-local`),
adds **Prism** to the Start menu, Applications or the application menu, and opens the Home
page. Running it again updates Prism. `PRISM_DIR` chooses another folder.

On the Home page, **Getting started** does the rest, one click each: the GitHub CLI, your
GitHub login, LaTeX and, if you want it, the AI assistant. For LaTeX, **Quick start**
downloads [Tectonic](https://tectonic-typesetting.github.io/) (one program; packages come
on the first compile, which needs the internet), and **Full LaTeX** installs MiKTeX
(Windows); a TeX you already have is used as it is. Settings lists the same programs later.

### By hand

The steps the one line does, for those who want to choose.

#### 1. Prerequisites

Python 3.9 or newer and a TeX distribution. Prism also uses git (every version of your
papers) and the GitHub CLI (your papers on GitHub): you do not need to know either, and
the Home page installs them for you (**Getting started**, one click each). To install
everything yourself instead:

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

#### 2. Get prism-local

```sh
git clone https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex.git prism-local
```

Without git: on the [repository's page](https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex)
choose **Code → Download ZIP** and unpack it into a folder named `prism-local`. A copy made
with git updates itself; a downloaded one does not (download it again for a new version).
That is the whole installation: there is nothing to build.

#### 3. Start it

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

- **GitHub**: on the Home page, **Getting started** installs the GitHub CLI and logs you in:
  **Log in to GitHub** shows a code, which you enter on the GitHub page it opens (once).
  Projects can then get a private repository each (or one per folder), and every change is
  saved there by itself. On another computer, **From GitHub…** copies a project of yours
  there and keeps it in sync. (In a terminal, `gh auth login` does the same login.)
- **Claude Code** for the agent panel: **Getting started** (or Settings → Programs)
  installs it and opens a window where you log in once. In a terminal:
  `irm https://claude.ai/install.ps1 | iex` on Windows,
  `curl -fsSL https://claude.ai/install.sh | bash` elsewhere, then run `claude` once to log in.
  [Codex](https://github.com/openai/codex), DeepSeek's
  [Deep Code](https://github.com/lessweb/deepcode-cli) (`npm install -g @vegamo/deepcode-cli`)
  or an API key work too; see
  [Choosing the AI](#choosing-the-ai-claude-code-codex-deepseek-and-other-apis).

### Update

prism-local updates itself: when a server starts (an editor or the Home page), it takes
what is new on GitHub in the background, at most once every five minutes; opening a project
never waits for it, and the editor then offers a restart. It only fast-forwards, and leaves the
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
- A right-click (or the **⋯** in the preview pane) pins, renames, moves to a folder, tags, opens
  the folder in Explorer/Finder, copies the folder or its path, or removes from the list (the
  files are never touched). **Move folder to Recycle Bin…** deletes the project's folder to the
  Recycle Bin (Trash on macOS) and forgets it; close its editor first. It refuses a folder that
  holds your home folder, prism-local or another project.
- **Copy folder** puts the project's folder on the system clipboard, to paste in Explorer or
  Finder. **Paste project folders** (right-click the empty space or a folder) adds folders
  copied there as projects, where they are; nothing is copied.
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
- **Changes in Explorer / Finder**: Windows directory notifications and macOS FSEvents
  trigger reconciliation. Home receives server-sent events and updates automatically;
  it does not poll the project list or periodically scan directories. A missing path triggers a recovery
  scan once, and a persistent missing path is not searched repeatedly. Startup and changes
  to the registered directory set also reconcile once. On other platforms, missing-path
  recovery is available without automatic discovery notifications. A group whose name matches its projects' containing
  disk folder is linked automatically (shown as **Disk folder** in its preview).
  Inside these containers, new projects with `main.tex` or `prism.json` appear with their
  subfolders. Same-volume moves and renames within the watched directory trees keep the
  project's tags, pinned state and history. Deleted or unavailable folders stay listed as
  missing, so restoring them works; projects explicitly removed from Home stay removed.
  Hidden folders, build output and directory links are excluded. For a move outside these
  trees or onto another drive, use **Add existing…** at the new location.

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

## Research baseline

The Agent panel offers **Initialize** when a project has no research baseline.
Opening a project does not start analysis. **Later** keeps reading, discussion and
manual editing available; reopen **Research baseline** whenever you are ready.
Until initialization, API providers and the controlled Claude Code, Codex and Deep Code adapters remain read-only and hold newly authored
mathematical chat content. Existing mathematical quotations can still be discussed.

Initialization records the main manuscript and included chapters, their content
versions and locations, explicitly marked terms, and complete mathematical notation.
Existing notation needs no second approval. Empty projects can start from a research
question, explicitly supplied notation, or empty libraries. The report lists unread
and excluded files, uncertain commands, and other extraction gaps. Missing definitions
remain unresolved; conflicting definitions remain visible.

The report supports corrections without editing the manuscript. Its versioned project
record is saved in `research/knowledge.json` and follows the existing file sync setting.
No additional Git workflow is started.

After initialization, maintenance checks the original source snapshots for missed
notation and reports each addition with its original version and location. Verified
editor input can also register newly introduced notation, including the editor's
controlled snippet expansions. Equivalent spellings such as `x_i` and `x_{i}` share
an identity. Ordinary powers, standard operators and formatting commands do not add
names. Changing the use of a known symbol does not ask for notation approval.

New notation from external saves, disk changes or reference material remains
unverified. Saving unrelated prose or whitespace does not adopt names that
were already in the file. The report shows unverified notation, evidence, extraction
gaps and maintenance notices; unchanged state produces no repeated notice or write.
The editor keeps input capabilities in page memory and checks exact saved versions.
Registered input receipts require both a local signature and a matching local editor
witness. Copied project fields, signatures or Git author metadata are insufficient.

GitSync carries initialization records, libraries, configuration and existing
research documents through its ordinary save, upload, receive and conflict paths.
It does not establish who adopted a received name. Prism checks initialization and
human-decision evidence separately. A forged baseline or an `approved` field does
not grant notation permission, and an older or missing project record does not
erase locally verified decisions or refusals.

Quoted instructions remain discussion. Markdown fences, indented code and quoted
HTML cannot approve notation. State a decision in ordinary unquoted text when the
editor cannot distinguish it from a quotation.

When received notation has no applicable evidence, the editor keeps the source
unchanged and asks an ordinary confirmation question with its location and version.
Reply normally to one clear question, or name the individual items in a batch.
The editor checks the source version again before saving adoption. A changed source
or ambiguous answer remains unverified. Confirmation records the author's adoption;
it does not claim that the original remote edit was human. Unknown definitions stay
unresolved. No AI task starts merely because a record arrives.
An explicit request such as `Reopen $z$` reopens a rejected received name for a new
source-bound question. Reopening alone does not approve its use.

A synchronized research topic keeps its identity and document when another working
folder explicitly initializes and continues it. Received text stays marked as an
unverified reference; later local entries carry separate sources. Reconnecting
GitHub still requires this editor's repository and account settings. A verified
existing topic issue is continued rather than duplicated.

Records from another working folder remain visible as unverified references when
their local evidence cannot be checked. Use **Initialize** to establish this folder's
baseline explicitly. Conflicted or damaged records remain intact and require the
existing conflict-resolution path; source verification does not perform another
merge or force synchronization on. Pending saves and verification state survive
restart without repeating unchanged notices.

API providers and the controlled Claude Code, Codex and Deep Code adapters check a completed draft before writing a source file or displaying
mathematical chat. A new name produces a saved pending candidate with its definition,
scope, reason and original expression. The proposal ends the Turn and restores
**Send**. It does not adopt the candidate or continue a derivation with it. Unsolicited
new notation is held with a request for the missing details. Tool previews and provider
reasoning do not grant permission. Received unknown expressions can remain in their
original argument; copying or rewriting them requires review.

Protected model compilation runs the entire build in a disposable copy under a verified macOS
sandbox. Without that runtime it fails closed. Custom builds, Lua, shell escape,
dynamic TeX and script inputs return diagnostics only. Static ordinary builds may
publish a PDF and log after checking the input versions. Source,
SyncTeX and dependency receipts are not copied back. Manual editor builds retain their
existing behavior.

This protects the controlled API, Claude Code, Codex, Deep Code and editor input paths. Arbitrary localhost clients,
browser automation and unrestricted host execution remain outside that boundary.
For one complete proposal at the end of a Turn, reply normally in the same chat:
for example, **Yes, I approve** or **No, I reject**. The editor saves the original
human message and decision before allowing use. An unrelated or ambiguous reply,
quotation, attachment, model output, Stop or reload does not approve notation.
The report and candidate cards show saved approval or rejection. Pending and decided
notation remain available after restart; API Conversation history also restores
within the same project and provider configuration. A history-save failure is reported.

A batch keeps each item separate. For example, **Approve z; reject w; use q instead
of v** decides only those items. A bare yes does not approve the rest of a batch.
Clear clauses can be saved while an ambiguous clause asks only for its missing details.
A complete direct instruction such as **Use h for x+x** adopts the author's notation
without inventing an Agent proposal or asking for a second approval.

Finite subscript families use explicit permitted indices: **Use $a_i$ for $x+x$
with i in {1,2,3}** permits the complete names $a_1$, $a_2$ and $a_3$. Other bases,
indices and new decorations remain subject to review. Ordinary powers of a known
name remain ordinary mathematics. These are declarative rules, not arbitrary regexes
or executable macros.

**Reject z** rejects that name. **Reject z and keep its expression expanded** also
prevents another letter from abbreviating that source expression. **Keep $x+x$
expanded; do not abbreviate it** can record the same preference without a proposal.
Refusals persist. **Reopen z** returns a rejected proposal to pending, without
adopting it; a complete explicit adoption or replacement records the new human choice.
Contradictory clauses, quotations, conditions, silence and a topic change grant nothing.
Checks normalize supported literal notation spellings; they do not prove arbitrary
mathematical equivalence. The report exposes item status, finite rules, refusal scope
and the original human evidence. Saving fails before any of these permissions apply.

Controlled Claude Code uses the same knowledge and decision rules, including batches,
direct instructions, finite families and durable refusals. Every Turn receives current
project knowledge. Its CLI stays in the original project directory with native session
ownership checks, but a verified macOS sandbox denies source writes, editor-private
state access and loopback connections. Only editor-owned SDK MCP tools can apply checked
edits. There is no rollback-based or unprotected fallback. If this runtime cannot be
verified, the controlled Claude entry point is unavailable.

Before launch, project/profile/scratch metadata is checked for pre-existing hardlinks;
a linked regular file, scan failure or limit (50000 entries / 5000 directories) refuses
the run without modifying links. The selected Claude auth/session profile and scratch
remain writable; installed runtime/system resources are trusted. Project/user settings
that launch hooks or grant native file/shell permissions are not loaded in this flow.
WebSearch/WebFetch still use the editor's existing Web grants. Native CLI inference,
authentication and real-service acceptance are separate from protocol fixtures.

After initialization, meaningful discussion updates are recorded automatically in one
continuing Markdown document per topic under `research/topics/`. Ideas, confirmed
author decisions and open questions remain separate, with their original sources.
Recording a possible proof method does not launch research or edit the manuscript.
The chat and Research panel show an openable location after the record is saved.
Unchanged discussion does not create another file or repeat the saved notice.

Topic summaries use the same notation checks as chat and manuscript edits. Recording
a decision in a topic does not adopt notation. A failed save remains pending for retry;
reopening the initialized project's Research panel can recover it without another
model request. If the topic file changed meanwhile, its received text is kept until
the conflict is resolved. Topic documents follow existing GitSync settings, including
disabled sync and delays during an active Turn.

To keep discussions in GitHub, choose **GitHub issues** in the Research panel's
**Discussion mode** and save the settings. This uses the project's confirmed GitHub
repository and the existing `gh` login from Home. Save the designated human approval
accounts there; those settings do not approve any notation.

Each topic has one continuing issue. Later updates become comments on that issue,
preserving its coauthors' edits. The panel distinguishes a local save, an update
waiting to publish, a confirmed publication and an unknown publication result.
After an unknown result, Prism checks the existing remote records rather than
blindly creating another issue or comment. A result remains unknown until it can be
confirmed. Local documents and configuration still follow ordinary GitSync settings.

GitHub discussion checks run while a project page is open. Closing the last page
pauses them; reopening checks for missed changes. Received comments remain labelled
external discussion. Their actual author and version are retained, and mathematical
previews use the shared notation checks. Comments do not launch research or edit the
manuscript.

For GitHub notation approval, designate the human accounts in **Discussion settings**.
In the research report, choose an existing topic and the complete pending proposals
to share. The editor publishes an explicitly pending proposal batch in that topic's
continuing issue. Sharing a proposal does not approve it. If no topic exists yet,
record the research topic in chat first.

One designated human account can decide the published proposals with an explicit
reply. Each batch item is independent; a bare yes does not approve an entire batch.
A reply to the only unambiguous batch needs no special command. When several batches
are open, include the displayed `prism-batch:<batch-id>` line to identify the batch.
An old reply cannot approve a later proposal version. Actual comment authorship and
repository, issue, project and content versions are checked; quotations, undesignated
accounts, Bot accounts and copied project status fields do not grant permission.

Verified GitHub decisions use the same notation, family and refusal rules as chat
decisions. The editor saves the original comment evidence and each decision before
later Turns can use the notation. The report shows the author and source-comment
link. A failed registration leaves the proposal pending and retryable. Repeated
checks and reopening do not duplicate a saved decision or start an AI task.

Controlled providers use the shared topic tool; their capability labels describe
the verified runtime and adapter paths rather than the entire research feature set.

## History and sync with GitHub

You do not need to know git. Create a private GitHub repository for a project from the
GitHub button (or **+ GitHub** on the Home page), and from then on Prism saves every change
there by itself:

- your changes are saved to the project's history two minutes after you stop editing (at
  the latest ten minutes after the first), and every agent turn is saved on its own, with
  your request as its description;
- on Windows and macOS, native filesystem events also schedule edits made by another
  editor or an external Codex session while the project is open. They use the same
  automatic commit and upload flow; build output and git metadata do not schedule saves;
- everything saved is uploaded to GitHub, and what changed on GitHub (another computer, a
  co-author) is brought in: right when the project opens, then every five minutes. Where
  you and a co-author changed the same lines, both versions are kept for you to choose;
- when the Home page starts, it looks at GitHub for every project in the background (no
  file changes) and marks the projects with new changes *↓ N new on GitHub*; opening the
  project brings them in;
- the GitHub button at the top says where things stand, and has **Save to GitHub now**,
  **Get changes from GitHub** and a switch, **Save to GitHub by itself**. The two actions
  work with the switch off too;
- the **History** tab lists every version of the open file, shows what each changed, and
  restores one; the **Diff** tab shows what is not saved to the history yet, or what the
  last save changed.

Prism keeps the repositories it set up in order itself. If another program changes how
such a project is connected to GitHub, Prism puts it back and goes on. It never uploads
somewhere other than the GitHub repository the project belongs to: if the project is
pointed at a different repository, uploading pauses (your edits stay saved on this
computer) and the GitHub button says so in plain words, and it goes on by itself once the
project points back. **Sync with the new one** in that menu makes the new repository the
project's.

Files changed on disk by other programs (Claude Code in a terminal, another editor) reload
by themselves, and a save never silently overwrites a newer version on disk.

**For those who know git.** Each project syncs one branch with one remote branch, bound in
`.git/prism-local.json` when Prism creates the repository or, for one you set up yourself,
when it first syncs. In a repository you set up, Prism does not change your settings: a
changed remote, branch, push URL, URL rewrite, pushRemote/pushDefault or mirror pauses
upload until it names the bound target again, or you choose the new one. Details of every
pause are under **Details** in the GitHub menu.

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

## Snippets

Short triggers expand into LaTeX as you type, after
[obsidian-latex-suite](https://github.com/artisticat1/obsidian-latex-suite). In math:

| You type | You get |
|---|---|
| `@a` `@b` `@G` …, or `alpha`, `theta`, `infty`, `partial` … | `\alpha` `\beta` `\Gamma` …, `\alpha`, `\theta`, `\infty`, `\partial` |
| `x2` · `xsr` · `xrd` · `xhat` · `xbar` | `x_{2}` · `x^{2}` · `x^{}` · `\hat{x}` · `\bar{x}` |
| `1/x` Tab · `//` · `sq` | `\frac{1}{x}` · `\frac{}{}` · `\sqrt{}` |
| `sum` · `lim` · `dint` · `par` Tab | `\sum_{i=1}^{n}` · `\lim_{n \to \infty}` · `\int_{0}^{1} \, dx` · `\frac{\partial y}{\partial x}` |
| `<=` `!=` `->` `=>` `xx` `**` `inn` `RR` `NN` | `\leq` `\neq` `\to` `\implies` `\times` `\cdot` `\in` `\mathbb{R}` `\mathbb{N}` |
| `lr(` · `norm` · `avg` · `pmat` · `cases` | `\left( \right)` · `\lvert \rvert` · `\langle \rangle` · a `pmatrix` · `cases` |
| select text, then `S` `F` `U` `C` | `\sqrt{…}` `\frac{…}{}` `\underbrace{…}_{}` `\cancel{…}` |

In text: `mk` gives `$ $`, `dm` gives `\[ \]`, `beg` Tab gives `\begin{…} \end{…}` (the
name is typed once, for both). The ⚡ menu above the editor lists them all.

- **Tab** jumps to the next field of the snippet (Shift-Tab back). With no snippet, it leaves
  the bracket or the math the cursor is in, and in `align`, `matrix`, `cases` … it puts `&`.
  There, **Enter** ends the row with `\\` (Shift-Enter: a plain new line).
- **Ctrl-Z** right after a snippet brings back what you typed.
- Math is found as LaTeX writes it: `$ $`, `$$ $$`, `\( \)`, `\[ \]`, `equation`, `align`,
  `gather`, … Inside `\text{}` it is text again. Comments and the arguments of `\label`,
  `\ref`, `\cite`, `\begin` … get no snippets, and neither does a command you type out
  (`\sum`, `\alpha`).
- **Home → Settings → Snippets in the editor** turns snippets, auto-fraction and the `&`/`\\`
  keys on and off, for every project. An editor already open takes a change when you go back to it.

**Your own snippets** go in `~/.prism-local/snippets.js` (Home → Settings → Edit my snippets): an array of
`{trigger, replacement, options}`, in the format of obsidian-latex-suite. They come before the
built-in ones; `{snippets: [...], defaults: false}` drops the built-in ones.

The app also distributes `prism_local/shared-snippets.js` with its updates. Without a
local snippets file, every project uses this bundle. An existing local file (including
one selected by `PRISM_SNIPPETS`) replaces the bundle completely; it is never overwritten
by an app update. Saving in Home → Settings creates or updates that local override. To
return to the distributed rules, remove or rename the local snippets file.

To distribute later changes to your local rules, run `python bin/export-snippets.py`
from the app checkout, review `prism_local/shared-snippets.js`, and commit and push it
with the app update. This copies the configured local file into the tracked bundle;
it does not change the local file or publish anything by itself.

Typing `\begin{` opens environment candidates (for example `equation`, `align`,
`itemize` and `enumerate`). Type a prefix, select with the arrow keys and confirm with
Enter or Tab to insert the matching `\end{...}` and move into the indented body.
Escape cancels. An existing matching end is reused.

| Option | Meaning |
|---|---|
| `t` / `m` (`M` display, `n` inline) | only in text / only in math |
| `A` | expand as you type (otherwise on Tab) |
| `r` | the trigger is a regular expression; `[[0]]`, `[[1]]` … in the replacement are its groups |
| `v` | on a selection: the trigger is the key typed, `${VISUAL}` is the selection (a replacement with `${VISUAL}` is one even without `v`) |
| `w` | only after a word boundary |

In the replacement, `$0`, `$1` … are the fields Tab visits, `${1:text}` one with text in it
(the same number twice is typed once). A replacement can be a function of the match.
`${GREEK}`, `${SYMBOL}`, `${ACCENT}` and `${MORE_SYMBOLS}` in a trigger stand for those names. The file is
JavaScript and runs in the editor page: write it yourself, or read before you paste.

## Keyboard

| Key | Action |
|---|---|
| Tab / Shift-Tab | Expand a snippet, next / previous field, out of a bracket, `&` in a row |
| ⌘S / Ctrl-S | Save now (edits are saved automatically anyway) |
| ⌘↵ / Ctrl-Enter | Save all and compile |
| ⌘J / Ctrl-J | Show the cursor line in the PDF |
| double-click or Ctrl/⌘-click in PDF | Jump to the source line |
| click a link in PDF / Alt+← | Follow a reference, citation, contents entry or URL / go back |
| ⌘F / Ctrl-F | Find in the open file (Enter: next, Shift+Enter: previous, Esc: close; Alt+C match case, Alt+W whole word, Alt+R regular expression) |
| ⌘⌥F / Ctrl-H | Find and replace in the open file (Enter: replace this one; Replace all is one undo) |
| ⌘⇧F / Ctrl-Shift-F | Search the project, in the sidebar; Replace all there changes every file |
| ↓ / ↑ in the results | Go through the project's results (or the agent's changes), each shown in its file; Enter: edit there; Space: tick or untick a change; ↑ on the first goes back to the search box. In the find box, ↓ ↑ are next and previous |
| Ctrl-G | Go to a line, or line:column (also: click "Ln, Col" under the editor) |
| ⌘F / Ctrl-F after clicking the PDF | Search the PDF (Enter: next, Shift+Enter: previous, Esc: close) |
| Ctrl-Q | Fold or unfold the section, environment or `\[ … \]` at the cursor |
| Ctrl-K Ctrl-0 / Ctrl-K Ctrl-J | Fold all / unfold all |
| ⌘L / Ctrl-L | Ask Claude about the selection |
| ⌘/ / Ctrl-/ | Toggle `%` comments |
| ⌘B / Ctrl-B | Show/hide the file sidebar |
| Ctrl-Space | Completion |

Under the editor, the status line shows the cursor's line and column (and how many characters
are selected); a click there asks for a line to go to.

In a regular expression, the replacement may use `$1`, `$2` … for its groups and `$&` for the
whole match. Search in the project includes unsaved edits of open files. Its Replace all
changes open files in their tabs (⌘Z there takes it back) and the others on disk, each only
if nothing changed it since it was read.

**Search with the agent.** For a change a plain search cannot express ("rename the index i
to j in every formula", "write `\mathscr{O}` instead of `\mathcal{O}`"), open Search
(⌘⇧F / Ctrl-Shift-F), choose **✦ With the agent** and describe it. The agent chosen in the
agent panel reads the project in a read-only turn of its own (not in your conversation) and
proposes rules (a regular expression, its replacement, and where it applies: in formulas,
in the text, or anywhere) and, where no rule fits, single changes. Prism finds every place
each rule matches and lists every change with its line, before and after; untick what should
stay, or edit a rule and the list follows. **Apply** makes the ticked changes as Replace all
does. Nothing changes before you apply.

## Building

**Compile** (⌘↵) saves every file. It builds the active `.tex` file if that file contains
`\documentclass`. For a chapter or another file, it builds the root document from `prism.json`.
The PDF pane and pop-out viewer follow the compiled document. If that document has no PDF,
the preview clears. The last-build details show the file name. The built-in builder:

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
- `files`: globs for source scanning (outline and completion). By default every
  `.tex/.bib/.md/.sty/.cls/.bbx/.cbx/.txt/.tikz` file is scanned. The Files sidebar also lists
  assets. Hidden files and directories, `outdir` and `node_modules` are skipped.
- `exclude`: globs to hide from the file tree.

Changes to `prism.json` apply at once, without restarting. A `prism.json` that cannot be used
is reported in the Compile menu and the build output, and the defaults apply.

## The agent panel

Each project and provider has its own conversation. The server checks the project before
starting a turn and checks a saved CLI session before resuming it. If an old session's
project cannot be verified, start a new chat. Old saved records are kept.

This section describes the panel with Claude Code, the default. The next section covers the
other providers and what differs for them.

Each protected message runs Claude Code headlessly in the original project, behind a
verified read-only source boundary. Native tools are restricted to WebSearch/WebFetch.
The editor serves project reads, checked edits, proposals and guarded compilation over
the existing bidirectional SDK MCP control channel. Prompt text cannot grant a file
write or notation approval. The selected native session, model and effort still apply.

**Model and thinking.** The conversation header lists available models and the selected
model's thinking effort levels. Claude and Codex read their CLI metadata; API providers
query `/models`. Refresh the list with **↻**. Changing model or effort keeps the same
conversation, and every subsequent request uses that choice. The server saves choices
in the user settings as defaults for later sessions. The controls are disabled during a turn.
If an API's model list omits effort capabilities, configure `model_efforts` below.

**LaTeX references.** Click labels such as `\ref{eq:main}`, `\cref{thm:one,lem:two}`,
or a known label name in an Agent reply to open its source line. The index covers project
source files. Clicking refreshes the index and the target tab. Duplicate labels offer a
choice of locations; missing labels show a message.

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

- **With @-mentions**, the editor-owned tools may change only the mentioned files.
  They check scope before applying a draft; the CLI itself cannot write source. For a
  line range, Claude is asked to keep to those lines.
- **Without @-mentions**, edits default to the file open when you send the message.
  Explicit requests to edit other files take priority. The compile main file is not an
  automatic edit target. This default guides the agent; it retains permission to edit
  other project files and create files. If no file is open and the target is unclear,
  the agent is instructed to ask which file to edit.
- The line above the message box shows scope before you send. The final card reports
  checked edits and blocked operations; **Undo this turn** can revert accepted edits.
- Each message states its own scope, so a limit from an earlier message does not carry over.

Consequences:

- Put writing conventions in the project's read-only `CLAUDE.md`. Project/user executable
  settings and external MCP servers do not expand the protected editor's tool authority.
- **Edit** mode can change source only through the editor-owned tools and their checks.
  Native shell, file-write and delegated Agent tools are not available.
- **Web permissions** appear in the conversation when Claude requests WebSearch or
  WebFetch. Allow once, allow for this conversation, always allow, or deny. Approval
  continues the original blocked request in the same CLI process. Saved grants are
  confined to the project, CLI profile and tool; WebFetch grants cover one public HTTP
  origin. The editor's tool, scope and runtime restrictions still apply. Saved grants live
  in `web-permissions.json` in Prism's user state folder, outside the project.
- **The agent compiles** through the shared guarded compile tool: isolated inputs,
  checked artifact publication and diagnostics. The manual Compile button keeps its own
  behavior. API models use the same guarded model-tool path.
- Adding shell permissions in `.claude/settings.json` does not expand this protected
  workflow. Refused operations appear in the Turn's result.
- **Ask** mode is read-only. Its tool list omits source writes and compilation, and the
  editor rechecks mode even if a malformed CLI requests one.
- The active file's path is sent with each message; selected source text is sent when
  you @-mention it.
- **Add files** to a message with **+**, by dragging them onto the panel, or by pasting an
  image. They are saved in the project's `prism-uploads/` folder (up to 25 MB each) and the
  protected Claude transport sends text as reference data and images/PDFs as structured
  content. Attachments never substitute for the independent human decision message.
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
  - They synchronize after each message and periodically while the panel is visible.
  - **↻** always starts a new read-only `get_usage` query, without a model message.
  - The panel shows the last successful sync time. A failed query keeps the previous
    snapshot marked stale; a changed account/profile cannot reuse it. Cached snapshots
    expire after 60 seconds or when a reported reset time passes.
  - Older CLIs without `get_usage`, and API/cloud authentication, show usage as unavailable.
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

**Codex CLI.** In editor projects, controlled turns use the local app-server stdio
protocol in the original project directory. Editor-owned tools reuse the API's file
and notation checks. The native CLI cannot write the manuscript, access editor
approval evidence or reach editor loopback HTTP. Streaming mathematical text and
visible tool diagnostics are checked before publication; proposals end the Turn.
Ordinary human replies use the same saved single, batch, direct, family and refusal
rules as API and Claude Code. Each Turn loads current knowledge and enforces its
Ask/Edit mode, mentioned-file scope and active target.

New controlled Conversations install parent-owned dynamic tools. Older native
Conversations keep their id and history and use a required stdio MCP relay to the
same parent checker. The relay uses per-Turn local pipes, with no HTTP endpoint or
human-approval operation. Native or restored tools cannot bypass the source write
boundary. Hooks, apps, external MCP servers and native command tools stay disabled;
configuration that cannot be verified fails closed. Supported controlled execution
currently requires the verified macOS sandbox. Other systems do not fall back to an
unprotected editor Turn. Manual editing and manual compilation remain available.
These are controlled-protocol capabilities; real model and installed-App acceptance
need separate evidence.

When project research access is absent, the legacy path runs `codex exec --json`
with an isolated named permissions profile, and `resume` continues the native
conversation. Codex reads your `AGENTS.md`. `/effort` sets `model_reasoning_effort`
(`minimal` … `xhigh`). Edit grants native write permission only to the @-mentioned files,
or the project tree when there are no mentions. `.git`, `.codex`, `.agents` and `.aws`
metadata remain read-only, including nested metadata already present when the turn starts.
Mentioning a file inside these directories stops the turn with an explanation.
Metadata a turn creates is not covered; for every agent, the turn's result names any such
directory that appeared anywhere in the project. Nothing is deleted: remove it if unexpected.
Ask grants no project writes and no compile tool. The adapter requires a CLI supporting
named profiles, `--ignore-user-config` and `--ignore-rules` on both new and resumed turns;
it checks compatibility and login before a turn.
It supplies only the editor's compile MCP, without loading user MCP configuration,
and turns Codex hooks off for editor turns (`features.hooks=false`): `--ignore-user-config`
alone still runs hooks from `~/.codex/hooks.json`. Your terminal hook settings are unchanged.
On Windows the adapter explicitly selects the native `elevated` sandbox, since ignoring
user configuration would otherwise also discard `windows.sandbox` and block shell reads.
Set up that sandbox once in the Codex CLI or desktop app before using the editor agent.
Commands in that sandbox run as a separate sandbox user, which cannot start programs
installed only for your account (for example a per-user Python under
`%LOCALAPPDATA%/Programs`). Compiling is unaffected: the compile tool runs in the editor.
On Windows it also finds the Codex desktop app's bundled `codex.exe` under
`%LOCALAPPDATA%/OpenAI/Codex/bin`, even when a GUI launcher has no Codex on its
`PATH`. `CODEX_BIN` and a provider's `bin` setting take precedence. The bundled
path is resolved again after app updates; no versioned path needs to be saved.
Codex reads and searches files with its native shell tools and edits with `apply_patch`.
The editor sends these tool instructions on every turn, including resumed conversations.
The [Windows sandbox audit](docs/audits/2026-10-15-codex-windows/report.md) records the cause,
related fixes, offline native tests and validation limits.

**Deep Code (DeepSeek's terminal agent).** [Deep Code](https://github.com/lessweb/deepcode-cli)
uses a controlled exec path in editor projects. It requires the verified macOS
sandbox and support for `--settings`, `--json` and `--session-id`. The editor passes
per-Turn settings from a temporary file, retains the original HOME, project cwd and
native Conversation, and never overlays the project's settings or rewrites history.
Its stdio MCP child uses the same checked parent tools as the other controlled CLIs.
Native writes cannot modify the manuscript or editor approval evidence, even when
Deep Code's category checks do not restrict a command.

Each Turn sends current research knowledge. A proposal is saved and ends the CLI
before another generation request; the next ordinary human message uses the shared
single, batch, direct, family, refusal and reopening rules. Mathematical output and
native tool-log previews are checked before display. Only unchanged parent-applied
writes enter the Turn's diff and Undo; other saves and later manual edits stay intact.
Stale whole-file drafts require another parent read before application. Stop cleans
up the CLI and descendants.

The replacement settings disable inherited MCP servers and make notification and
custom Web scripts inert. Existing configuration and manual edits stay in place.
Deep Code's native MCP call limit remains 60 seconds; the relay's longer transport
budget does not extend it. Compile-route tests use controlled diagnostics, with no
claim of a real model or TeX acceptance run. Unverified runtimes fail closed.

Without project research access, the legacy CLI path runs
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
  `model_efforts` (per-model effort lists, for example
  `"model_efforts": {"my-reasoner": ["low", "high"], "my-fast-model": []}`),
  `timeout` (seconds), `enabled`. Per-model levels override provider-wide `efforts`.
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
  and the build directory are excluded. The one file outside it is your snippets file
  (`~/.prism-local/snippets.js`), which the editor page runs as JavaScript.
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
The macOS app uses the same launcher. With Chrome or Safari as the default browser, it
selects an existing Home tab without reloading it. If no matching tab is open, it opens one.

A shortcut that opens one project's editor directly, skipping the Home page:

```powershell
powershell -ExecutionPolicy Bypass -File launcher\make-shortcut.ps1 -Project D:\path\to\paper
python3 launcher/make_desktop_entry.py --project ~/papers/my-paper --desktop      # Linux
```

- **Options.** `make-shortcut.ps1` takes `-Name`, `-NoDesktop`, `-Folder` (where to put the
  shortcut), `-Browser` and `-Python` (the Python whose `pythonw.exe` runs it); `make_desktop_entry.py` takes `--name`, `--browser`, `--desktop`
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
- This is a lightweight launcher. Open it from Finder, Spotlight, or the Dock. It reuses a
  running Home server and exits after opening or selecting the browser page.
- The app uses the Python that ran the installer and the repository in its current location.
  Keep an external drive connected if the repository is on that drive. Run the installer
  again after moving the repository or replacing Python. Updates from `git pull` are loaded
  when the Home and editor servers restart.
- Running the installer again updates its existing app. It refuses to replace an app with
  a different bundle identifier or a symbolic link.
- The app adds the usual Homebrew, TeX Live and user CLI folders to `PATH`. Provider settings
  and project data remain in the locations listed above.
- macOS may ask to let prism-local control Chrome or Safari so it can select an existing tab.
  If this access is denied, or another browser is the default, it opens a new tab instead.
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
install.ps1, install.sh    the one-line install (Windows; macOS and Linux)
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
prism_local/sharedrepo.py  a folder's projects in one shared repository, or one each, and back
prism_local/registry.py    shared state: project list, running instances, ports
prism_local/presence.py    which pages are open, for --exit-when-idle
prism_local/agent.py       agent turns for every provider: scope, per-turn diffs and undo
prism_local/backends.py    the backend interface, presets and ~/.prism-local/agents.json
prism_local/backend_*.py   Claude Code, Codex CLI, Deep Code CLI and OpenAI-compatible API backends
prism_local/mcp_compile.py the agent's compile tool (MCP, stdio): builds through the editor server
prism_local/gitsync.py     commits, pushes and pulls the project's changes (History, sync)
prism_local/repoview.py    where a project is in git and what changed in it, asked again only on a change
prism_local/selfupdate.py  updates prism-local itself from GitHub when a server starts
prism_local/tools.py       git, the GitHub CLI, LaTeX and Claude Code: found, or installed from Home
prism_local/static/        front end (app.js, pdfview.js, viewer.*, home.*, common.js, snippets.js, findlogic.js, agentsearch.js, app.css)
prism_local/static/vendor/ CodeMirror 5.65.18 (MIT), PDF.js 3.11.174 (Apache-2.0), with local CMaps for CJK PDFs,
                           marked 12.0.2 (MIT) and KaTeX 0.16.11 (MIT) for the agent's replies
examples/minimal/          a small amsart project to try it on
tests/                     python -m unittest discover -s tests
```

## Limitations

- SyncTeX lookup is a compact reimplementation, not the `synctex` library. It is accurate to the
  line for ordinary text and math. Inside complex constructs (tables, TikZ, floats) it can land a few lines off.
- The built-in builder does not run xindy or bib2gls (xindy-style glossaries and indexes,
  glossaries-extra's `\GlsXtrLoadResources`). Use `"builder": "latexmk"` with a latexmkrc
  for those.
- No collaborative editing. Right-click the Files sidebar to create a file or folder, rename,
  copy and paste, delete to the Recycle Bin (Trash on macOS), or show an item in Explorer or
  Finder; drag files onto it to add them. Copy and Paste use the system clipboard, so files go
  to and from Explorer, Finder and other projects (on Linux, only within the project).
  Pasting never brings a `.git` or other metadata folder along, and a name that is taken gets
  `-copy`. None of this runs during an agent's turn. Empty folders are listed too.

## License

MIT, see [LICENSE](LICENSE). The vendored third-party libraries keep their own licenses: see
`prism_local/static/vendor/LICENSE-*`. The snippets follow the design and most of the default
snippets of [obsidian-latex-suite](https://github.com/artisticat1/obsidian-latex-suite) (MIT).
The PDF.js CMaps keep their license in `prism_local/static/vendor/cmaps/LICENSE`.

"Prism" in the name refers to the general idea of an AI-assisted LaTeX workspace. This
project is not affiliated with OpenAI's Prism or with Anthropic.
