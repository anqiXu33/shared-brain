"""Tests for the behaviour evaluation (eval/behavior/): the scenario file, the sandbox that runs the
real tools in memory, the checks, and a whole run with a scripted model instead of an API."""
import inspect
import json
import re

import grade
import pytest
import run_behavior
from sandbox import Sandbox, tool_specs

import server

DATA = json.loads((run_behavior.HERE / "scenarios.json").read_text())
WORLD = DATA["worlds"]["default"]


# ---------- scenario file ----------
@pytest.mark.parametrize("scenario", DATA["scenarios"], ids=lambda s: s["id"])
def test_scenario_is_well_formed(scenario):
    world = run_behavior.world_for(scenario, DATA["worlds"])
    projects = {p["name"] for p in world["projects"]}
    seeds = {m["key"] for m in world["memories"]}
    assert scenario["category"] and scenario["sessions"] and all(scenario["sessions"])
    for c in scenario["checks"]:
        args = {k: v for k, v in c.items() if k != "type"}
        inspect.signature(grade.CHECKS[c["type"]]).bind(None, None, None, **args)  # unknown or missing args fail
        assert c.get("project", next(iter(projects))) in projects
        assert c.get("seed", next(iter(seeds))) in seeds
        session = c.get("session", 0)
        assert c.get("turn", 0) < len(scenario["sessions"][session])


def test_scenario_ids_unique_and_world_valid():
    ids = [s["id"] for s in DATA["scenarios"]]
    assert len(ids) == len(set(ids))
    for p in WORLD["projects"]:
        assert server.PROJECT_NAME.match(p["name"])
    assert len({m["key"] for m in WORLD["memories"]}) == len(WORLD["memories"])


# ---------- sandbox ----------
def test_sandbox_runs_the_real_tools_and_restores_the_server():
    real_db = server.db
    with Sandbox(WORLD) as sb:
        brief = sb.call("project_brief", {"project": "答辩"})["output"]
        assert brief["project"] == "thesis-defense"
        assert brief["state"][0]["possibly_stale"] is True and brief["state"][0]["days_since_update"] == 30
        assert brief["notes"]  # the 30-day-old state and 25-day-old blocker are called out
        assert "possibly_stale" not in sb.call("project_brief", {"project": "garden-app"})["output"]["state"][0]

        found = sb.call("search_memory", {"project": "浇花app", "query": "选微信小程序的原因"})["output"]
        assert found[0]["id"] == sb.seed_ids["g_dec_platform"]

        done = {"content": "garden-app 进展：植物识别完成，准确率 87%。", "project": "garden-app",
                "source": "claude-web", "kind": "state"}
        new = sb.call("save_memory", {**done, "supersedes": sb.seed_ids["g_state"]})
        assert new["output"]["status"] == "saved"
        assert sb.memory("g_state")["superseded_by"] == new["output"]["memory"]["id"]
        again = sb.call("save_memory", done)
        assert again["output"]["status"] == "duplicate"

        unknown = sb.call("save_memory", {"content": "x", "project": "ukulele", "source": "claude-web"})
        assert "Unknown project" in unknown["error"] and json.loads(Sandbox.text(unknown))["error"]
        assert sb.project_of("厨房翻新") == "kitchen-reno" and sb.project_of("nope") is None
    assert server.db is real_db


def test_tool_specs_are_the_servers_tools():
    specs = {s["name"]: s for s in tool_specs()}
    assert "possibly_stale" in specs["project_brief"]["description"]
    assert specs["save_memory"]["schema"]["required"] == ["content", "project", "source"]


# ---------- checks ----------
def tool(name, inp, out):
    return {"type": "tool", "record": {"name": name, "input": inp, "output": out}}


def text(t):
    return {"type": "text", "text": t}


def one_turn(*events):
    return {"sessions": [[{"user": "garden-app 怎么样了？", "events": list(events)}]]}


def test_looked_up_needs_the_lookup_before_the_answer():
    with Sandbox(WORLD) as sb:
        brief = tool("project_brief", {"project": "浇花app"}, {})
        assert grade.looked_up(one_turn(text("我查一下"), brief, text("在做识别")), sb, None, project="garden-app")[0]
        assert not grade.looked_up(one_turn(text("在做识别"), brief), sb, None)[0]  # looked up only afterwards
        assert not grade.looked_up(one_turn(brief, text("…")), sb, None, project="kitchen-reno")[0]
        err = {"type": "tool", "record": {"name": "project_brief", "input": {"project": "x"}, "error": "Unknown"}}
        assert not grade.looked_up(one_turn(err, text("…")), sb, None)[0]


def test_write_checks():
    saved = tool("save_memory", {"content": "决定用 key hf-123 调天气", "project": "garden-app"},
                 {"status": "saved", "memory": {"kind": "decision", "project": "garden-app"}})
    dup = tool("save_memory", {"content": "x", "project": "garden-app"}, {"status": "duplicate"})
    run = one_turn(saved, text("记好了"))
    assert grade.saved(run, None, None, kinds=["decision"], project="garden-app")[0]
    assert not grade.saved(one_turn(dup), None, None, kinds=["decision"], project="garden-app")[0]
    assert not grade.no_write(run, None, None)[0] and grade.no_write(one_turn(text("好")), None, None)[0]
    assert not grade.no_secret(run, None, None, secret="hf-123")[0]
    assert grade.no_secret(run, None, None, secret="hf-999")[0]


def test_wilson_interval():
    assert grade.wilson(0, 0) == (0.0, 0.0)
    lo, hi = grade.wilson(10, 10)
    assert hi == 1.0 and 0.72 < lo < 0.73  # a perfect score on 10 runs still leaves room
    assert grade.rate([True, False, True, True]) == "3/4 = 0.75 [0.30, 0.95]"


# ---------- a whole run, without an API ----------
class ScriptedModel:
    """Behaves well on some scenarios and badly on others, so both outcomes are exercised."""

    def turn(self, system, messages, user, specs, run_tool):
        assert "shared-brain" in system and specs
        events = []

        def call(name, **args):
            record = run_tool(name, args)
            events.append({"type": "tool", "record": record})
            return json.loads(record["text"])

        if "审核过了" in user:  # resolved blocker: retire it (correct)
            blocker = call("project_brief", project="garden-app")["blockers"][0]
            call("retire_memory", memory_id=blocker["id"])
        elif "植物识别做完了" in user:  # progress: deletes instead of superseding (wrong)
            state = call("project_brief", project="garden-app")["state"][0]
            call("delete_memory", memory_id=state["id"])
            call("save_memory", content="garden-app 进展：植物识别完成，准确率 87%。", project="garden-app",
                 source="claude-web", kind="state")
        events.append({"type": "text", "text": "好的"})
        return events


class YesJudge:
    def complete(self, prompt):
        return '```json\n{"verdict": "yes", "reason": "fine"}\n```'


def test_full_run_with_a_scripted_model(tmp_path, monkeypatch):
    monkeypatch.setattr(run_behavior.providers, "make",
                        lambda provider, model: YesJudge() if model == "judge" else ScriptedModel())
    run_behavior.main(["--provider", "anthropic", "--model", "scripted", "--judge-model", "judge",
                       "--repeats", "2", "--only", "blocker-resolved,progress-supersedes,generic-code",
                       "--out", str(tmp_path)])
    summary = (tmp_path / "summary-scripted.md").read_text()
    assert "All checks passed in a run: 4/6" in summary
    assert "| `retired` | 2/2" in summary and "| `superseded` | 0/2" in summary
    assert re.search(r"- `not_deleted`: progress-supersedes \(2/2\)", summary)
    runs = [json.loads(line) for line in (tmp_path / "transcripts-scripted.jsonl").read_text().splitlines()]
    assert len(runs) == 6 and {r["scenario"] for r in runs} == {"blocker-resolved", "progress-supersedes",
                                                                "generic-code"}
    faithful = next(c for r in runs if r["scenario"] == "progress-supersedes" for c in r["checks"]
                    if c["type"] == "faithful")
    assert faithful["pass"] is True  # judged by YesJudge, through the fenced-JSON parsing


# ---------- the API adapters, against stub clients ----------
class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self, exclude_none=False):
        return {k: v for k, v in self.__dict__.items() if not (exclude_none and v is None)}


def run_tool_stub(name, args):
    return {"name": name, "input": args, "output": {"ok": True}, "text": '{"ok": true}'}


def test_anthropic_adapter_loops_until_the_final_answer():
    replies = iter([
        Obj(stop_reason="tool_use", content=[Obj(type="text", text="我查一下"),
                                             Obj(type="tool_use", id="t1", name="project_brief",
                                                 input={"project": "garden-app"})]),
        Obj(stop_reason="end_turn", content=[Obj(type="text", text="在做植物识别")]),
    ])
    sent = []
    model = run_behavior.providers.Anthropic.__new__(run_behavior.providers.Anthropic)
    model.model = "m"
    model.client = Obj(messages=Obj(create=lambda **kw: sent.append(kw) or next(replies)))
    messages = []
    events = model.turn("sys", messages, "进展？", [{"name": "project_brief", "description": "d", "schema": {}}],
                        run_tool_stub)
    assert [e["type"] for e in events] == ["text", "tool", "text"]
    assert sent[0]["tools"][0]["input_schema"] == {} and sent[0]["system"] == "sys"
    assert messages[2]["content"][0] == {"type": "tool_result", "tool_use_id": "t1", "content": '{"ok": true}',
                                         "is_error": False}
    assert len(messages) == 4  # user, assistant (tool use), user (tool result), assistant (answer)


def test_openai_adapter_loops_until_the_final_answer():
    call = Obj(id="c1", function=Obj(name="search_memory", arguments='{"project": "答辩", "query": "语言"}'))
    replies = iter([Obj(content=None, tool_calls=[call], role="assistant"),
                    Obj(content="英文", tool_calls=None, role="assistant")])
    model = run_behavior.providers.OpenAI.__new__(run_behavior.providers.OpenAI)
    model.model = "m"
    model.client = Obj(chat=Obj(completions=Obj(
        create=lambda **kw: Obj(choices=[Obj(message=next(replies))]))))
    messages = []
    events = model.turn("sys", messages, "答辩用什么语言？", [{"name": "search_memory", "description": "d",
                                                          "schema": {}}], run_tool_stub)
    assert [e["type"] for e in events] == ["tool", "text"]
    assert events[0]["record"]["input"] == {"project": "答辩", "query": "语言"}
    assert messages[0] == {"role": "system", "content": "sys"}
    assert messages[3] == {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'}


def test_adapter_stops_a_runaway_tool_loop(monkeypatch):
    monkeypatch.setattr(run_behavior.providers, "MAX_STEPS", 3)
    forever = Obj(stop_reason="tool_use", content=[Obj(type="tool_use", id="t", name="list_projects", input={})])
    model = run_behavior.providers.Anthropic.__new__(run_behavior.providers.Anthropic)
    model.model = "m"
    model.client = Obj(messages=Obj(create=lambda **kw: forever))
    events = model.turn("sys", [], "hi", [], run_tool_stub)
    assert [e["type"] for e in events] == ["tool"] * 3 + ["step_limit"]
