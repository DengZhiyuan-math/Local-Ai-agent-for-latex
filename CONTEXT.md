# Prism Local Agent

The Agent helps a user read and edit a LaTeX project through a conversation.

## Language

**Turn**:
One user request and the Agent's work until it finishes or the user stops it.

**Conversation**:
The history shared by turns for one project and provider. Changing its model preserves that history.

**Web grant**:
The user's approval for WebSearch or for WebFetch at one HTTP origin. A grant applies once, to one conversation, or persistently to one project and profile.

**Usage snapshot**:
Subscription usage measured at the last successful synchronization. A failed synchronization does not give an old snapshot a new timestamp.

**LaTeX label**:
A named source location declared with a LaTeX label command. Multiple declarations of the same name are ambiguous.

**Research baseline**:
The author's explicitly initialized account of the manuscript's terms and complete notation, with source versions, locations, unresolved definitions, and extraction gaps. Initialization records existing notation without asking for approval or rewriting the manuscript.

**Complete notation**:
A mathematical identifier including its decorations. For example, `\ell` and `\ell_+` are distinct notations; a LaTeX formatting command is not itself an identifier.

**Research topic**:
A continuing account of related ideas, confirmed author decisions, and open questions,
with their sources. Recording a discussion does not instruct the Agent to carry out
research or authorize mathematical notation.

**Received topic reference**:
A continuing topic's identity, location and synchronized text retained as an
unverified reference. Continuing it preserves the received text and records new
local statements with their own sources. It does not import another working
folder's human authority or notation permission.

**GitHub discussion binding**:
The author's choice of a confirmed GitHub repository for a project's continuing
research topics. Connection settings and designated human accounts identify where
discussion belongs; they do not approve any particular notation.

**External discussion**:
A received issue comment with its repository, author and version. It is reference
material, not an editor instruction or a verified notation decision.

**GitHub notation decision**:
A designated human account's explicit reply to an exact published proposal or
batch. The editor verifies the actual comment author, repository, topic issue and
proposal version, then preserves the original comment before registering each
decision in the shared notation library. A discussion mirror is not this evidence.

**Verified manual edit**:
An editor input change whose private capability matches the exact saved file versions and whose issued receipt has a local editor witness. A generic save, an Agent write, or Git author metadata does not establish this source.

**Unverified notation**:
Notation found in the current manuscript without applicable baseline or verified adoption evidence. Its presence in a file or reference does not itself authorize AI adoption.

**Received research record**:
Project knowledge received through file synchronization. Transport preserves its
content; local source verification determines which parts can establish notation
permission. Git authorship and copied status fields do not establish human adoption.

**Author-confirmed received notation**:
An author's explicit adoption of a received name after an editor question bound to
its current source location and version. This confirms adoption, not who originally
edited the remote source. An unknown definition remains unresolved.

**Notation proposal**:
A versioned pending candidate with its intended definition, scope, reason and source expression. Saving or displaying a proposal does not adopt the notation. In a controlled workflow, a proposal ends the Turn before the candidate is used.

**Notation decision**:
The author's approval or rejection of a complete proposal version. A batch has
independent decisions for each item. A clear reply identifies its current subject;
quotations, unrelated replies and silence grant no adoption. The original human
message remains part of the evidence.

**Direct notation instruction**:
The author's complete instruction to use a notation, independently of an Agent
proposal. A clear replacement can inherit the discussed proposal's definition and
scope. An ambiguous part remains unresolved without changing other items.

**Notation family**:
A set of complete identifiers allowed by an explicit rule. A finite subscript family
fixes its base and permitted indices; other names and decorations stay outside it.
An ordinary numerical power is an operation on a known identifier.

**Notation refusal**:
An enduring choice against one complete name or against abbreviating a specified
expression. Changing letters does not override an expression refusal. An explicit
reopening allows reconsideration without adoption; an explicit replacement records
the author's new choice while preserving earlier evidence.

**Controlled CLI Turn**:
A CLI conversation whose manuscript changes are performed by the editor only after
shared policy checks. The CLI may request operations and present candidates, but it
cannot write the manuscript or supply human adoption evidence itself.
