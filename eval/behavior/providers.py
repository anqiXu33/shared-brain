"""The two model APIs the evaluation can drive, behind one small interface.

    turn(system, messages, user_text, specs, run_tool) -> events
        Adds one user message to `messages` (kept per conversation, in the API's own format),
        lets the model call tools until it gives a final answer, and returns what happened:
        [{"type": "tool", "record": {...}}, {"type": "text", "text": "..."}, ...] in order.
    complete(prompt) -> str
        One plain request without tools, used by the judge.

Anthropic reads ANTHROPIC_API_KEY, OpenAI reads OPENAI_API_KEY (both from the environment or .env).
"""
import json

MAX_STEPS = 12  # tool rounds per user message before the turn is cut off and graded as it stands


class Anthropic:
    def __init__(self, model: str):
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model

    def turn(self, system, messages, user_text, specs, run_tool):
        tools = [{"name": s["name"], "description": s["description"], "input_schema": s["schema"]} for s in specs]
        messages.append({"role": "user", "content": user_text})
        events = []
        for _ in range(MAX_STEPS):
            resp = self.client.messages.create(model=self.model, max_tokens=2048, system=system,
                                               tools=tools, messages=messages)
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type == "text":
                    events.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    record = run_tool(block.name, block.input)
                    events.append({"type": "tool", "record": record})
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": record["text"], "is_error": "error" in record})
            if resp.stop_reason != "tool_use":
                return events
            messages.append({"role": "user", "content": results})
        events.append({"type": "step_limit"})
        return events

    def complete(self, prompt):
        resp = self.client.messages.create(model=self.model, max_tokens=512,
                                           messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in resp.content if b.type == "text")


class OpenAI:
    def __init__(self, model: str):
        import openai

        self.client = openai.OpenAI()
        self.model = model

    def turn(self, system, messages, user_text, specs, run_tool):
        tools = [{"type": "function", "function": {"name": s["name"], "description": s["description"],
                                                   "parameters": s["schema"]}} for s in specs]
        if not messages:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_text})
        events = []
        for _ in range(MAX_STEPS):
            msg = self.client.chat.completions.create(model=self.model, messages=messages,
                                                      tools=tools).choices[0].message
            messages.append(msg.model_dump(exclude_none=True))
            if msg.content:
                events.append({"type": "text", "text": msg.content})
            if not msg.tool_calls:
                return events
            for call in msg.tool_calls:
                record = run_tool(call.function.name, json.loads(call.function.arguments))
                events.append({"type": "tool", "record": record})
                messages.append({"role": "tool", "tool_call_id": call.id, "content": record["text"]})
        events.append({"type": "step_limit"})
        return events

    def complete(self, prompt):
        msg = self.client.chat.completions.create(model=self.model,
                                                  messages=[{"role": "user", "content": prompt}])
        return msg.choices[0].message.content


def make(provider: str, model: str):
    return {"anthropic": Anthropic, "openai": OpenAI}[provider](model)
