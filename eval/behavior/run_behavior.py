"""Behaviour evaluation: does an assistant actually use the shared memory the way it is told to?

    python eval/behavior/run_behavior.py --provider anthropic --model claude-sonnet-5-5
    python eval/behavior/run_behavior.py --provider openai --model <model> --repeats 5
    python eval/behavior/run_behavior.py ... --only stale-flag,secret     # a few scenarios

Each scenario gives the model the same standing instructions as the real clients
(instructions.md), the real tool descriptions from server.py, and a private copy of the memory
(sandbox.py). The user's messages are scripted; a scenario can have several conversations, which
share the memory but not the chat history. Afterwards grade.py checks what the model did.

Writes eval/behavior/results/summary-<model>.md (aggregate numbers, safe to commit) and
transcripts-<model>.jsonl (every message and tool call, for reading failures; gitignored).
"""
import argparse
import copy
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent.parent)]

import grade  # noqa: E402
import providers  # noqa: E402
from sandbox import Sandbox, tool_specs  # noqa: E402


def system_prompt() -> str:
    return (HERE / "instructions.md").read_text() + f"\n\nToday is {date.today().isoformat()}."


def world_for(scenario: dict, worlds: dict) -> dict:
    world = copy.deepcopy(worlds[scenario.get("world", "default")])
    world["memories"] += scenario.get("extra_memories", [])
    return world


def run_scenario(scenario: dict, worlds: dict, model, judge, system: str, specs: list[dict]) -> dict:
    with Sandbox(world_for(scenario, worlds)) as sb:
        def run_tool(name, args):
            record = sb.call(name, args)
            record["text"] = Sandbox.text(record)
            return record

        run = {"sessions": []}
        for user_turns in scenario["sessions"]:
            messages, turns = [], []
            for user in user_turns:
                turns.append({"user": user, "events": model.turn(system, messages, user, specs, run_tool)})
            run["sessions"].append(turns)
        run["checks"] = grade.grade(run, scenario["checks"], sb, judge)
    return run


def summarize(results: list[dict], scenarios: dict, args) -> str:
    by_category, by_check, failures = defaultdict(list), defaultdict(list), defaultdict(lambda: defaultdict(int))
    overall = []
    for r in results:
        applicable = [c for c in r["checks"] if c["pass"] is not None]
        ok = all(c["pass"] for c in applicable)
        overall.append(ok)
        by_category[scenarios[r["scenario"]]["category"]].append(ok)
        for c in applicable:
            by_check[c["type"]].append(c["pass"])
            if not c["pass"]:
                failures[c["type"]][r["scenario"]] += 1
    lines = [
        "# Behaviour evaluation",
        "",
        f"Model: `{args.provider}:{args.model}`. Judge: `{args.judge_provider}:{args.judge_model}`. "
        f"{len(scenarios)} scenarios × {args.repeats} runs, {date.today().isoformat()}. "
        "Rates with 95% Wilson intervals.",
        "",
        f"**All checks passed in a run: {grade.rate(overall)}**",
        "",
        "| Category | Runs passed |",
        "|---|---|",
        *[f"| {k} | {grade.rate(v)} |" for k, v in sorted(by_category.items())],
        "",
        "| Check | Passed |",
        "|---|---|",
        *[f"| `{k}` | {grade.rate(v)} |" for k, v in sorted(by_check.items())],
        "",
        "## Failures",
        "",
    ]
    if not failures:
        lines.append("None.")
    for check, where in sorted(failures.items()):
        lines.append(f"- `{check}`: " + ", ".join(f"{s} ({n}/{args.repeats})" for s, n in sorted(where.items())))
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--provider", choices=["anthropic", "openai"], required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--judge-provider", choices=["anthropic", "openai"], help="default: same as --provider")
    p.add_argument("--judge-model", help="default: same as --model")
    p.add_argument("--repeats", type=int, default=3, help="runs per scenario; models are not deterministic")
    p.add_argument("--only", help="comma-separated scenario ids")
    p.add_argument("--scenarios", type=Path, default=HERE / "scenarios.json")
    p.add_argument("--out", type=Path, default=HERE / "results")
    args = p.parse_args(argv)
    args.judge_provider = args.judge_provider or args.provider
    args.judge_model = args.judge_model or args.model

    data = json.loads(args.scenarios.read_text())
    scenarios = {s["id"]: s for s in data["scenarios"]}
    if args.only:
        scenarios = {i: scenarios[i] for i in args.only.split(",")}
    model = providers.make(args.provider, args.model)
    judge = providers.make(args.judge_provider, args.judge_model)
    system, specs = system_prompt(), tool_specs()

    args.out.mkdir(parents=True, exist_ok=True)
    results = []
    with (args.out / f"transcripts-{args.model}.jsonl").open("w") as log:
        for sid, scenario in scenarios.items():
            for i in range(args.repeats):
                run = run_scenario(scenario, data["worlds"], model, judge, system, specs)
                run.update(scenario=sid, repeat=i)
                results.append(run)
                log.write(json.dumps(run, ensure_ascii=False) + "\n")
                failed = [c["type"] for c in run["checks"] if c["pass"] is False]
                print(f"{sid} #{i + 1}: {'ok' if not failed else 'FAILED ' + ', '.join(failed)}", flush=True)
    summary = summarize(results, scenarios, args)
    (args.out / f"summary-{args.model}.md").write_text(summary)
    print("\n" + summary)


if __name__ == "__main__":
    main()
