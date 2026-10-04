import copy
import io
import json
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from mini_agent.context import ContextBuilder, ContextLimit
from mini_agent.model import ChatModel, ModelError, parse_message
from mini_agent.runtime import Agent
from mini_agent.store import SessionBusy, SessionStore
from mini_agent.tools import Tool, default_registry


def final(text):
    return {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": text,
    }}]}


def call(name, arguments, call_id="c1"):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)
        if not isinstance(arguments, str) else arguments,
    }}


def calls(*items):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "执行所需工具。", "tool_calls": list(items),
    }}]}


class ScriptModel:
    """Only used in deterministic tests, never a production decision maker."""
    def __init__(self, *script):
        self.script = iter(script)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append(copy.deepcopy(messages))
        response = next(self.script)
        if isinstance(response, Exception):
            raise response
        return parse_message(response)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "sessions.db")
        self.store = SessionStore(self.db)

    def agent(self, model, **kwargs):
        return Agent(model, default_registry(), self.store, trace_path=None, **kwargs)

    def test_direct_chat_and_followup(self):
        model = ScriptModel(final("你叫小林。"), final("小林。"))
        agent = self.agent(model)
        agent.run("u", "w", "我叫小林")
        result = agent.run("u", "w", "我叫什么？")
        self.assertEqual(result.status, "completed")
        self.assertTrue(any(m.get("content") == "我叫小林" for m in model.requests[-1]))
        self.assertEqual(len(self.store.load("u", "w")["history"]), 4)

    def test_multiple_steps_tool_results_and_followup(self):
        model = ScriptModel(
            calls(call("calculator", {"expression": "6*7"})),
            calls(call("todo", {"action": "add", "text": "答案是42"}, "c2")),
            final("已记录42。"),
            calls(call("todo", {"action": "complete", "id": 1}, "c3")),
            final("已完成。"),
        )
        agent = self.agent(model)
        result = agent.run("u", "w", "计算6*7并加入待办")
        observation = next(m for m in model.requests[1] if m["role"] == "tool")
        self.assertEqual(json.loads(observation["content"])["result"]["value"], 42)
        self.assertEqual([e["tool"] for e in result.trace if e["event"] == "tool"],
                         ["calculator", "todo"])
        agent.run("u", "w", "完成刚才那条待办")
        self.assertTrue(self.store.load("u", "w")["todos"][0]["done"])

    def test_sessions_users_and_restart_are_isolated(self):
        agent = self.agent(ScriptModel(
            calls(call("todo", {"action": "add", "text": "w1"})), final("ok"),
            calls(call("todo", {"action": "add", "text": "w2"})), final("ok"),
        ))
        agent.run("u", "w1", "记录w1")
        agent.run("u", "w2", "记录w2")
        restarted = SessionStore(self.db)
        self.assertEqual(restarted.load("u", "w1")["todos"][0]["text"], "w1")
        self.assertEqual(restarted.load("u", "w2")["todos"][0]["text"], "w2")
        self.assertEqual(restarted.load("other", "w1")["todos"], [])

    def test_failed_arguments_unknown_tool_and_division_can_recover(self):
        model = ScriptModel(
            calls(call("missing", {}, "a"), call("calculator", "{bad", "b"),
                  call("calculator", {"expression": "1/0"}, "c")),
            calls(call("calculator", {"expression": "8/2"}, "d")), final("4"),
        )
        result = self.agent(model).run("u", "w", "计算")
        self.assertEqual(result.status, "completed")
        errors = [json.loads(m["content"])["error"] for m in model.requests[1]
                  if m["role"] == "tool"]
        self.assertEqual(errors, ["unknown_tool", "invalid_arguments", "tool_failed"])

    def test_max_steps_preserves_all_call_pairs(self):
        agent = self.agent(ScriptModel(calls(call("calculator", {"expression": "1+1"}))), max_steps=1)
        result = agent.run("u", "w", "不断计算")
        self.assertEqual(result.status, "max_steps")
        history = self.store.load("u", "w")["history"]
        self.assertEqual(history[-2]["tool_call_id"], history[-3]["tool_calls"][0]["id"])
        self.assertEqual(history[-1]["role"], "assistant")

    def test_model_failure_after_write_and_lock_release(self):
        model = ScriptModel(calls(call("todo", {"action": "add", "text": "保留"})),
                            ModelError("模拟超时"), final("继续"))
        agent = self.agent(model)
        self.assertEqual(agent.run("u", "w", "记录").status, "model_error")
        self.assertEqual(self.store.load("u", "w")["todos"][0]["text"], "保留")
        self.assertEqual(agent.run("u", "w", "继续").status, "completed")

    def test_repeated_call_id_never_duplicates_todo(self):
        duplicate = calls(call("todo", {"action": "add", "text": "一次"}))
        result = self.agent(ScriptModel(duplicate, duplicate)).run("u", "w", "记录")
        self.assertEqual(result.status, "model_error")
        self.assertEqual(len(self.store.load("u", "w")["todos"]), 1)

    def test_same_session_busy_other_session_available(self):
        entered, release = threading.Event(), threading.Event()
        errors = []

        def hold():
            try:
                with self.store.claim("u", "a"):
                    entered.set()
                    release.wait(2)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=hold)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(SessionBusy):
                self.agent(ScriptModel(final("x"))).run("u", "a", "hello")
            self.assertEqual(self.agent(ScriptModel(final("y"))).run("u", "b", "hi").status, "completed")
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(errors)

    def test_trace_file_contains_metadata_not_prompt(self):
        path = str(Path(self.temp.name) / "trace.jsonl")
        agent = Agent(ScriptModel(calls(call("calculator", {"expression": "6*7"})), final("42")),
                      default_registry(), self.store, trace_path=path)
        agent.run("u", "w", "PRIVATE_PROMPT")
        raw = Path(path).read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE_PROMPT", raw)
        self.assertEqual(json.loads(raw.splitlines()[-1])["status"], "completed")

    def test_200_turn_compression_and_old_memory_recall(self):
        state = self.store.load("u", "w")
        for n in range(200):
            question = "项目代号海鸥，截止日期为十月十日" if n == 20 else f"普通问题{n}"
            state["history"] += [{"role": "user", "content": question},
                                 parse_message(calls(call("calculator", {"expression": "1+1"}, f"c{n}"))),
                                 {"role": "tool", "tool_call_id": f"c{n}", "content": '{"value":2}'},
                                 {"role": "assistant", "content": "已记录"}]
        state["history"].append({"role": "user", "content": "海鸥项目截止日期？"})
        messages, stats = ContextBuilder(max_chars=7000).build(state, default_registry().definitions())
        self.assertLessEqual(stats["chars"], 7000)
        self.assertGreater(stats["omitted_turns"], 0)
        self.assertIn(21, stats["recalled_turns"])
        self.assertIn("十月十日", messages[1]["content"])
        ids = {c["id"] for m in messages for c in m.get("tool_calls", [])}
        results = {m["tool_call_id"] for m in messages if m["role"] == "tool"}
        self.assertEqual(ids, results)
        self.assertEqual(len(state["history"]), 801)  # Compression does not delete storage.

    def test_context_overflow_reports_instead_of_breaking_protocol(self):
        state = self.store.load("u", "w")
        state["history"] = [{"role": "user", "content": "长" * 10000}]
        with self.assertRaises(ContextLimit):
            ContextBuilder(max_chars=6000).build(state, [])

    def test_large_todo_list_is_paginated_and_full_todos_remain(self):
        state = self.store.load("u", "w")
        state["todos"] = [{"id": n, "text": "待办" * 90, "done": False} for n in range(1, 101)]
        state["next_todo_id"] = 101
        self.store.save("u", "w", state)
        model = ScriptModel(calls(call("todo", {"action": "list"})), final("还有下一页"))
        self.agent(model).run("u", "w", "列出全部待办")
        observation = next(m for m in model.requests[-1] if m["role"] == "tool")
        result = json.loads(observation["content"])
        self.assertNotIn("truncated", result)
        self.assertEqual(result["result"]["total"], 100)
        self.assertIsNotNone(result["result"]["next_offset"])
        self.assertLess(len(observation["content"]), 3000)
        self.assertEqual(len(self.store.load("u", "w")["todos"]), 100)

    def test_input_limits(self):
        for user, session, text in [("", "w", "x"), ("u", "", "x"), ("u", "w", ""),
                                    ("u", "w", "x" * 4001)]:
            with self.subTest(user=user, session=session):
                with self.assertRaises(ValueError):
                    self.agent(ScriptModel()).run(user, session, text)


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.registry = default_registry()
        self.state = {"todos": [], "next_todo_id": 1}

    def execute(self, name, args):
        return self.registry.execute(name, json.dumps(args), self.state)

    def test_calculator_math_and_unsafe_input(self):
        self.assertEqual(self.execute("calculator", {"expression": "-(2+3)*4/2"})["result"]["value"], -10)
        for expression in ["__import__('os')", "True+1", "2**9999", "1e999", "[1][0]"]:
            self.assertFalse(self.execute("calculator", {"expression": expression})["ok"])

    def test_schema_rejects_extras_wrong_types_missing_and_large_input(self):
        for args in [{}, {"expression": 1}, {"expression": "1", "extra": 0},
                     {"expression": "1" * 161}]:
            self.assertEqual(self.execute("calculator", args)["error"], "invalid_arguments")
        self.assertEqual(self.execute("todo", {"action": "complete", "id": True})["error"], "invalid_arguments")

    def test_todo_crud_and_bad_operations(self):
        item = self.execute("todo", {"action": "add", "text": "阅读"})["result"].copy()
        self.assertEqual(item["id"], 1)
        self.assertEqual(len(self.execute("todo", {"action": "list"})["result"]["items"]), 1)
        self.assertTrue(self.execute("todo", {"action": "complete", "id": 1})["result"]["done"])
        self.execute("todo", {"action": "delete", "id": 1})
        self.assertFalse(self.execute("todo", {"action": "complete", "id": 1})["ok"])
        self.assertFalse(self.execute("todo", {"action": "add", "text": " "})["ok"])
        self.assertFalse(self.execute("todo", {"action": "delete"})["ok"])

    def test_search_is_explicit_mock(self):
        result = self.execute("search", {"query": "session"})["result"]
        self.assertTrue(result["mock"])
        self.assertEqual(result["items"][0]["id"], "session")
        self.assertEqual(self.execute("search", {"query": "xyzunknown"})["result"]["items"], [])

    def test_registration_and_unexpected_tool_exception(self):
        with self.assertRaises(ValueError):
            self.registry.register(Tool("todo", "重复注册", {"type": "object"}, lambda a, s: None))

        def broken(args, state):
            raise RuntimeError("INTERNAL_SECRET")

        self.registry.register(Tool("broken", "测试异常", {"type": "object"}, broken))
        result = self.execute("broken", {})
        self.assertEqual(result["error"], "internal_tool_error")
        self.assertNotIn("INTERNAL_SECRET", json.dumps(result))


class ProtocolTests(unittest.TestCase):
    def test_malformed_and_truncated_envelopes(self):
        examples = [{}, {"choices": []}, final(""), final(123), calls(call("todo", {}, "same"), call("todo", {}, "same"))]
        truncated = final("截断")
        truncated["choices"][0]["finish_reason"] = "length"
        examples.append(truncated)
        thinking = final("hello")
        thinking["choices"][0]["message"]["reasoning_content"] = "opaque"
        examples.append(thinking)
        bad_calls = final("hello")
        bad_calls["choices"][0]["message"]["tool_calls"] = {}
        examples.append(bad_calls)
        for payload in examples:
            with self.subTest(payload=payload):
                with self.assertRaises(ModelError):
                    parse_message(payload)

    def test_http_adapter_sends_schemas_and_parses_response(self):
        model = ChatModel("https://api.example.com/v1", "TEST_KEY", "test", thinking_disabled=True)
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(final("hello")).encode())) as http:
            result = model.complete([{"role": "user", "content": "hi"}], default_registry().definitions())
        request = http.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.example.com/v1/chat/completions")
        self.assertEqual(request.headers["Authorization"], "Bearer TEST_KEY")
        self.assertEqual(len(body["tools"]), 3)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(result["content"], "hello")

    def test_retry_and_sensitive_error_body_not_exposed(self):
        model = ChatModel("https://api.example.com", "TEST_KEY", "test")
        rate = urllib.error.HTTPError(model.url, 429, "rate", {}, None)
        with patch("urllib.request.urlopen", side_effect=[rate, io.BytesIO(json.dumps(final("ok")).encode())]) as http:
            with patch("time.sleep"):
                self.assertEqual(model.complete([], [])["content"], "ok")
        self.assertEqual(http.call_count, 2)
        denied = urllib.error.HTTPError(model.url, 401, "SECRET_PROMPT", {}, io.BytesIO(b"SECRET_PROMPT"))
        with patch("urllib.request.urlopen", side_effect=denied):
            with self.assertRaises(ModelError) as error:
                model.complete([], [])
        self.assertNotIn("SECRET_PROMPT", str(error.exception))

    def test_network_and_invalid_json_errors(self):
        model = ChatModel("https://api.example.com", "TEST_KEY", "test")
        with patch("urllib.request.urlopen", side_effect=TimeoutError()):
            with self.assertRaises(ModelError):
                model.complete([], [])
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"not-json")):
            with self.assertRaises(ModelError):
                model.complete([], [])


if __name__ == "__main__":
    unittest.main()
