# Behaviour evaluation

The [retrieval evaluation](../README.md) asks whether search finds the right memory. This one asks whether the assistant uses the memory at all, and uses it the way its instructions say:

| Category | What is checked | Scenarios |
|---|---|---|
| recall | looks the project up before answering (by name, alias, in English, mid-conversation, when asked for advice rather than status) and answers from what it found | 6 |
| ambiguous | asks which project is meant when two projects fit, instead of silently picking one | 1 |
| no-lookup | does not read or write memory for questions anyone could ask | 3 |
| write | saves a decision with its reason; does not save while the user is still deciding; does not register a new project unasked | 4 |
| supersede | replaces an outdated state or a changed decision with `supersedes` instead of adding a second one or deleting | 3 |
| retire | closes a resolved blocker with `retire_memory`; records a new blocker | 2 |
| privacy | a key mentioned in passing does not end up in memory | 1 |
| alias | records a new nickname for a project with `add_alias` | 1 |
| cross-session | something saved in one conversation is found in a new one | 3 |
| staleness | flags a month-old status as possibly outdated, but does not hedge a fresh one | 2 |

All scenarios use made-up projects (a plant-watering mini program, a thesis defence, a dialect speech-recognition experiment), so the file and the results can be public.

## How it works

- **Same instructions, same tools.** The system prompt is [`instructions.md`](instructions.md), the text in my Claude web settings. The tools are the server's own, with their real descriptions, read from `server.py`.
- **A private memory per run.** [`sandbox.py`](sandbox.py) loads the scenario's memories into an in-memory database ([`memdb.py`](memdb.py)) and runs the real tool functions against it: validation, supersede guards, duplicate check and `project_brief`'s staleness flags all behave as in production. Only search is simplified (texts are compared by shared characters), which is fine with a handful of memories and is not what this evaluation measures.
- **Scripted users.** Each scenario lists the user's messages, grouped into conversations that share the memory but not the chat history.
- **Grading** ([`grade.py`](grade.py)). Most checks are rules on the tool calls and on the memory at the end ("the old state is superseded, not deleted"). Two use a judge model that answers yes or no: whether an answer says the right thing, and whether every saved memory contains only what the user said or what was read from memory.
- **Repeated runs.** Models are not deterministic, so each scenario runs several times (3 by default); rates come with 95% Wilson intervals.

## Run it

Needs an API key for the model under test (`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, in `.env` or the environment) and `pip install anthropic` for Claude models. No Supabase or OpenAI embedding calls are made.

```bash
python eval/behavior/run_behavior.py --provider anthropic --model claude-sonnet-5-5
python eval/behavior/run_behavior.py --provider openai --model <model name>
python eval/behavior/run_behavior.py ... --only stale-flagged,fresh-not-hedged --repeats 5
```

The judge defaults to the model under test; `--judge-provider` and `--judge-model` set another one, which is better when comparing models.

Results go to `results/`: `summary-<model>.md` (rates by category and by check, and which scenarios failed) and `transcripts-<model>.jsonl` (every message, tool call and verdict; gitignored).

## Limits

- The prompt approximates a client, not reproduces it: Claude web and ChatGPT add their own system prompts, memory and tool-use habits. A failure here is a reason to look at the real client (with [`docs/roundtrip-checklist.md`](../../docs/roundtrip-checklist.md)), not proof that it fails there.
- 26 hand-written scenarios, in Chinese and one in English, drafted by the person who built the system.
- The judge is a model too. Its verdicts and reasons are in the transcripts; spot-check them.
