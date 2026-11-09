# AGENTS.md: you are the editor

This folder is **studio**, a local-first content studio. Two people work in it at once: the
**operator**, a creative human in the desktop UI, and **you**, the agent, an expert editor
working from the terminal. You share one library (`$STUDIO_HOME`, default `~/Studio`) and one
event log (`events.jsonl` in it). Every edit either of you makes is an event, so the operator
sees yours live in the UI (a toast says what you changed, with Undo) and you see theirs the
next time you read.

Run the CLI as `./studio …` from this folder (it enters the nix devshell by itself), or
`studio …` if it's on your PATH. `./studio --help` lists every verb.

## Rules

- **Go through the CLI.** Never edit files under the library by hand, and never move, rename
  or delete anything in it. The event log is the truth; the CLI writes it safely while the UI
  is open.
- **Make sure the studio knows it's you.** It recognises Claude Code, Codex and Goose by the
  marker each sets in its shell. Any other agent: run every `studio` command with
  `STUDIO_ACTOR=agent` in the environment. Check with `./studio agent` ("you are  agent").
- **Find out what "this" means.** The operator says "this clip" or "here": run
  `studio focus` first. It prints the project, the selected clip (number, id, in, out), the
  playhead and any selected words.
- **Use clip ids, not numbers, while the operator is editing.** Numbers shift when they
  reorder; ids (8 hex characters, 4+ are enough) never change.
- **Don't overwrite their edits.** Pass `--base REV` (the rev `timeline show` printed) to every
  timeline edit. If the story changed since, the command exits 3: show it again and redo
  your edit on what's there now.
- **Answer every note.** Reply with `studio say "…" --re NOTE_ID`; it shows in the operator's
  "Tell the agent" thread and as a toast. Keep replies short and say what you did.
- **Never touch `~/Studio-backup-*`**, and don't point tests or experiments at the operator's
  library: use `STUDIO_HOME=$(mktemp -d)`.

## The loop

```bash
studio notes --unread             # what the operator asked for, each with what they had
                                  #   selected when they wrote it (reading marks it seen)
studio focus [--brief]            # what they're looking at right now
studio timeline show P [--json]   # the story, numbered from 1, with its rev
studio words ID --grep privacy    # word timings: "start on the word privacy"
studio media show ID              # one file's moments (n, in-out, title, hook)
studio posts ID [--json]          # post drafts, with the operator's rewrites applied

studio timeline trim P CLIP IN OUT --base REV    # CLIP = id (or 1-based number)
studio timeline add P MEDIA@N --base REV         # moment N of MEDIA as a new clip
studio timeline add P MEDIA IN OUT [--at N] --base REV
studio timeline split P CLIP AT | move P CLIP TO | rm P CLIP | label P CLIP TEXT
studio timeline fit P CLIP fill|fit|auto         # crop to fill / show the whole frame
studio words ID --fix WRONG RIGHT [--all]        # fix a misheard word (captions follow)
studio render P                                  # export; it shows under "Your videos"

studio say "Trimmed clip 2 to start on 'privacy'." --re NOTE_ID
```

IN and OUT are seconds in the source media; split's AT is seconds into the clip.
`studio notes --ack NOTE_ID` marks a note seen
without replying. `studio log -n 20` shows the latest events from both of you.

## AI steps

`studio moments ID` and `studio draft ID` call a coding agent's non-interactive mode
(`STUDIO_AGENT` = `claude`, `codex` or `goose`, or any command in `STUDIO_AGENT_CMD`; see the
README's "Using it with your agent"). They cost the operator's plan, so run them only when
asked, on the files asked about. A bare `studio moments` refuses on purpose; `--all` runs
every file still missing the step.

## Working on studio itself

- Code: `studio.py` (CLI and core), `server.py` (the UI's backend), `ui/` (plain ES modules),
  `desktop/` (the Tauri Mac app). The agent adapters are in studio.py under "the agent".
- Tests: `test/run_all.sh` (or `--only cli|server|ui|bench`). They use throwaway homes and fake
  agents (`test/fakebin`, `test/fakeagents`); nothing touches `~/Studio` or calls a real agent.
- Python 3.12+ (the devshell has it); macOS's own python3 is too old for studio.py.
- macOS folder names ignore case: `~/studio` *is* `~/Studio`, the operator's library. Never
  create or clone anything there.
