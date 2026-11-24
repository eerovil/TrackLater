# Claude chats on the timeline

How the `claude` module turns Claude Code chat transcripts into hint entries
on their own timeline row. It shows the moments the person wrote a message, not
how long the agents worked, as one line of short blocks that never overlap.
Nothing is billed and no transcript text leaves the machine.

```mermaid
flowchart TD
    A[Fetch claude row for a date range] --> B[Read transcripts, one day wider on both sides]
    B --> F{Chat folder inside a client folder?}
    F -- No --> H{INCLUDE_UNGROUPED on?}
    H -- No --> X2[Skip chat]
    H -- Yes --> P
    F -- Yes --> P{Did the person write this message?}
    P -- No --> X3[Skip message]
    P -- Yes --> M[Put messages of all chats in one time order]
    M --> S{Same client, under IDLE since last, block under MAX_DURATION?}
    S -- Yes --> J[Join to the block]
    S -- No --> N[Start a new block]
    J --> E[Block ends TAIL after its last message, or where the next block starts]
    N --> E
    E --> R{Block starts inside the asked range?}
    R -- No --> X4[Leave out]
    R -- Yes --> L[Shown on the claude row]

    class A,B teal
    class F,H,P,S,R amber
    class M,J,N,E blue
    class L green
    class X2,X3,X4 rose
```

## Colours
- **teal**: reading the transcripts
- **amber**: a choice the module makes
- **blue**: building the blocks
- **green**: what the timeline shows
- **rose**: left out

## Boxes
- **Read transcripts, one day wider on both sides**: so a block has the same start however the range is cut, and a refetch cannot leave an overlapping copy. Folders come from `CLAUDE.global.DIR`, default `~/.claude*/projects`.
- **Chat folder inside a client folder?**: `group_for_cwd` and `group_dirs` in `tracklater/timemodules/claude.py`; unchanged.
- **INCLUDE_UNGROUPED on?**: off by default, as today.
- **Did the person write this message?**: typed messages, messages sent while the agent worked, and question box answers count. Messages AgentDeck sends by itself, tool results, summaries and subagent lines do not. Words AgentDeck wraps in a note of its own are unwrapped (`unwrap`).
- **Same client, under IDLE since last, block under MAX_DURATION?**: `IDLE` 300 seconds, `MAX_DURATION` 1800 seconds.
- **Block ends TAIL after its last message, or where the next block starts**: `TAIL` 120 seconds; also never past `MAX_DURATION`.
