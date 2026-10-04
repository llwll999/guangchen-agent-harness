"""The core receive → decide → act → observe loop."""

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .context import ContextBuilder, ContextLimit
from .model import ModelError
from .store import SessionStore
from .tools import ToolRegistry


@dataclass
class RunResult:
    answer: str
    status: str
    trace: list[dict]


def bounded_observation(result: dict, max_chars=3000) -> str:
    content = json.dumps(result, ensure_ascii=False, allow_nan=False)
    if len(content) <= max_chars:
        return content
    # Slicing raw text alone misses expansion when preview is JSON-escaped again.
    low, high = 0, len(content)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = json.dumps({"ok": result["ok"], "truncated": True,
                                "preview": content[:middle]}, ensure_ascii=False)
        if len(candidate) <= max_chars:
            low = middle
        else:
            high = middle - 1
    return json.dumps({"ok": result["ok"], "truncated": True,
                       "preview": content[:low]}, ensure_ascii=False)


class Agent:
    def __init__(self, model, registry: ToolRegistry, store: SessionStore,
                 context=None, max_steps=8, trace_path="data/trace.jsonl"):
        if type(max_steps) is not int or max_steps < 1:
            raise ValueError("max_steps 至少为 1。")
        self.model = model
        self.registry = registry
        self.store = store
        self.context = context or ContextBuilder()
        self.max_steps = max_steps
        self.trace_path = trace_path

    def run(self, user_id: str, session_id: str, text: str) -> RunResult:
        if (not isinstance(user_id, str) or not isinstance(session_id, str)
                or not user_id.strip() or not session_id.strip()
                or max(len(user_id), len(session_id)) > 100):
            raise ValueError("用户和会话 ID 不能为空且至多 100 字符。")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError("输入应为 1–4000 字符。")
        with self.store.claim(user_id, session_id):
            return self._run(user_id, session_id, text)

    def _run(self, user_id, session_id, text):
        state = self.store.load(user_id, session_id)
        state["history"].append({"role": "user", "content": text})
        run_id = uuid.uuid4().hex
        session_hash = hashlib.sha256(json.dumps([user_id, session_id]).encode()).hexdigest()[:12]
        trace = []

        def event(kind, **fields):
            trace.append({"time": datetime.now(timezone.utc).isoformat(),
                          "run_id": run_id, "session": session_hash,
                          "event": kind, **fields})

        # Detect repeated response IDs within this run before any duplicate side effect.
        seen_calls = set()
        status = "max_steps"
        answer = "已达到执行步数上限；已完成的工具操作已保存，可以继续追问。"
        for step in range(1, self.max_steps + 1):
            try:
                messages, stats = self.context.build(state, self.registry.definitions())
                event("context", step=step, **stats)
                message = self.model.complete(messages, self.registry.definitions())
            except (ModelError, ContextLimit) as exc:
                status = "model_error" if isinstance(exc, ModelError) else "context_limit"
                answer = str(exc) + " 本轮已完成的本地工具操作已保存。"
                event(status, step=step)
                break
            calls = message.get("tool_calls", [])
            if any(call["id"] in seen_calls for call in calls):
                status = "model_error"
                answer = "模型重复使用了工具调用 ID，已停止以避免重复执行。"
                event(status, step=step)
                break
            # Append the assistant envelope first, then one result for EACH call.
            state["history"].append(message)
            if not calls:
                answer = message["content"]
                status = "completed"
                break
            for call in calls:
                seen_calls.add(call["id"])
                fn = call["function"]
                started = time.monotonic()
                result = self.registry.execute(fn["name"], fn["arguments"], state)
                content = bounded_observation(result)
                state["history"].append({"role": "tool", "tool_call_id": call["id"],
                                          "content": content})
                event("tool", step=step, call_id=call["id"], tool=fn["name"],
                      ok=result["ok"], error=result.get("error"),
                      duration_ms=round((time.monotonic() - started) * 1000, 2))
        if status != "completed":
            state["history"].append({"role": "assistant", "content": answer})
        self.store.save(user_id, session_id, state)
        event("finished", status=status)
        if self.trace_path:
            try:
                Path(self.trace_path).parent.mkdir(parents=True, exist_ok=True)
                # One append write per run, no raw prompts, results or credentials.
                with open(self.trace_path, "a", encoding="utf-8") as log:
                    log.write("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in trace))
            except OSError:
                event("trace_write_failed")
        return RunResult(answer, status, trace)
