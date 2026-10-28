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
