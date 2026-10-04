"""Interviewer review: regressions, hostile data and state invariants."""

import copy
import json
import random
import tempfile
import io
import unittest
from pathlib import Path
from unittest.mock import patch

from mini_agent.context import ContextBuilder
from mini_agent.model import ChatModel, ModelError
from mini_agent.runtime import Agent
from mini_agent.store import SessionBusy, SessionStore
from mini_agent.tools import Tool, default_registry
from test_agent import ScriptModel, call, calls, final


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "review.db")
        self.store = SessionStore(self.db)
        self.state = self.store.load("u", "w")
        self.registry = default_registry()

    def test_failed_handler_rolls_back_local_state(self):
        def broken(args, state):
            state["todos"].append({"id": 1, "text": "ghost", "done": False})
            state["next_todo_id"] = 2
            raise ValueError("模拟写入一半失败")
        self.registry.register(Tool("broken", "failure", {"type": "object"}, broken))
        before = copy.deepcopy(self.state)
        self.assertFalse(self.registry.execute("broken", "{}", self.state)["ok"])
        self.assertEqual(self.state, before)

    def test_nonserializable_result_and_state_roll_back(self):
        for bad_state in (False, True):
            def broken(args, state):
                state["next_todo_id"] = 99
                if bad_state:
                    state["oops"] = float("nan")
                    return "ok"
                return object()
            name = f"bad_{bad_state}"
            self.registry.register(Tool(name, "bad JSON", {"type": "object"}, broken))
            before = copy.deepcopy(self.state)
            with self.subTest(bad_state=bad_state):
                self.assertFalse(self.registry.execute(name, "{}", self.state)["ok"])
                self.assertEqual(self.state, before)

    def test_definitions_cannot_change_execution_schema(self):
        definitions = self.registry.definitions()
        definitions[0]["function"]["parameters"]["properties"]["expression"]["maxLength"] = 999
        result = self.registry.execute("calculator", json.dumps({"expression": "1" * 161}), self.state)
        self.assertEqual(result["error"], "invalid_arguments")

    def test_registration_copies_caller_schema(self):
        schema = {"type": "object", "additionalProperties": False}
        self.registry.register(Tool("custom", "custom", schema, lambda a, s: a))
        schema["additionalProperties"] = True
        self.assertFalse(self.registry.execute("custom", '{"extra":1}', self.state)["ok"])

    def test_tool_result_cannot_mutate_committed_state(self):
        result = self.registry.execute("todo", '{"action":"add","text":"原文"}', self.state)
        result["result"]["text"] = "外部篡改"
        self.assertEqual(self.state["todos"][0]["text"], "原文")

    def test_duplicate_json_keys_are_rejected_before_write(self):
        raw = '{"action":"add","text":"甲","text":"乙"}'
        result = self.registry.execute("todo", raw, self.state)
        self.assertFalse(result["ok"])
        self.assertEqual(self.state["todos"], [])

    def test_nonfinite_json_is_rejected_before_handler(self):
        invoked = []
        self.registry.register(Tool("number", "number", {
            "type": "object", "properties": {"n": {"type": "number"}},
            "required": ["n"], "additionalProperties": False,
        }, lambda a, s: invoked.append(a) or "accepted"))
        for raw in ('{"n":NaN}', '{"n":Infinity}', '{"n":1e999}'):
            with self.subTest(raw=raw):
                self.assertFalse(self.registry.execute("number", raw, self.state)["ok"])
        self.assertEqual(invoked, [])

    def test_two_store_instances_share_session_exclusion(self):
        other = SessionStore(str(Path(self.db).parent / "." / "review.db"))
        with self.store.claim("u", "w"):
            with self.assertRaises(SessionBusy):
                with other.claim("u", "w"):
                    self.fail("同一会话不应并发进入")
            with other.claim("u", "another"):
                pass
        with other.claim("u", "w"):
            pass

    def test_memory_database_is_rejected_explicitly(self):
        with self.assertRaises(ValueError):
            SessionStore(":memory:")

    def test_escaped_tool_preview_respects_serialized_budget(self):
        self.registry.register(Tool("large", "large", {"type": "object"},
                                    lambda a, s: "\\\"\n\t\x00" * 2000))
        model = ScriptModel(calls(call("large", {})), final("done"))
        Agent(model, self.registry, self.store, trace_path=None).run("u", "w", "大结果")
        content = next(m["content"] for m in model.requests[-1] if m["role"] == "tool")
        self.assertLessEqual(len(content), 3000)
        self.assertTrue(json.loads(content)["truncated"])

    def test_late_old_answer_fact_is_recalled(self):
        self.state["history"] = [
            {"role": "user", "content": "介绍项目"},
            {"role": "assistant", "content": "普通背景。" * 400 + "海鸥项目密码为蓝鲸777。"},
        ]
        for n in range(6):
            self.state["history"] += [{"role": "user", "content": f"其他问题{n}"},
                                      {"role": "assistant", "content": "其他答案"}]
        self.state["history"].append({"role": "user", "content": "海鸥项目密码是什么？"})
        messages, stats = ContextBuilder().build(self.state, self.registry.definitions())
        self.assertIn(1, stats["recalled_turns"])
        self.assertIn("蓝鲸777", messages[1]["content"])

    def test_pagination_retrieves_every_escaped_todo_exactly(self):
        self.state["todos"] = [{"id": n, "text": ("\\\"\n\t\x00" * 40), "done": bool(n % 2)}
                               for n in range(1, 101)]
        self.state["next_todo_id"] = 101
        offset, found = 0, []
        for _ in range(101):
            result = self.registry.execute("todo", json.dumps({
                "action": "list", "offset": offset, "limit": 10,
            }), self.state)
            self.assertTrue(result["ok"])
            content = json.dumps(result, ensure_ascii=False, allow_nan=False)
            self.assertLessEqual(len(content), 3000)
            self.assertNotIn("truncated", json.loads(content))
            found.extend(result["result"]["items"])
            next_offset = result["result"]["next_offset"]
            if next_offset is None:
                break
            self.assertGreater(next_offset, offset)
            offset = next_offset
        else:
            self.fail("分页必须有限结束")
        self.assertEqual(found, self.state["todos"])

    def test_invalid_input_types_fail_before_loading_or_model(self):
        agent = Agent(ScriptModel(), self.registry, self.store, trace_path=None)
        for values in ((None, "w", "hi"), ("u", 1, "hi"), ("u", "w", {})):
            with self.subTest(values=values), self.assertRaises(ValueError):
                agent.run(*values)
        self.assertEqual(self.store.load("u", "w")["history"], [])

    def test_seeded_500_todo_operations_preserve_state_invariants(self):
        rng = random.Random(20261003)
        expected = {}
        next_id = 1
        for n in range(500):
            action = rng.choice(["add", "complete", "delete"])
            if action == "add":
                text = rng.choice(["中文", "emoji🙂", "quote\"", "slash\\", "多行\n内容"]) + str(n)
                result = self.registry.execute("todo", json.dumps({"action": action, "text": text}), self.state)
                self.assertTrue(result["ok"])
                expected[next_id] = {"id": next_id, "text": text, "done": False}
                next_id += 1
            else:
                item_id = rng.choice(list(expected) + [next_id + 20])
                before = copy.deepcopy(self.state)
                result = self.registry.execute("todo", json.dumps({"action": action, "id": item_id}), self.state)
                if item_id not in expected:
                    self.assertFalse(result["ok"])
                    self.assertEqual(self.state, before)
                elif action == "delete":
                    self.assertTrue(result["ok"])
                    del expected[item_id]
                else:
                    self.assertTrue(result["ok"])
                    expected[item_id]["done"] = True
            self.assertEqual(self.state["todos"], list(expected.values()))
            self.assertEqual(self.state["next_todo_id"], next_id)

    def test_failed_write_then_successful_write_and_model_error(self):
        def broken(args, state):
            state["todos"].append({"id": 77, "text": "不应保存", "done": False})
            raise ValueError("失败")
        self.registry.register(Tool("broken", "broken", {"type": "object"}, broken))
        model = ScriptModel(calls(call("broken", {}, "a"),
                                  call("todo", {"action": "add", "text": "应保存"}, "b")),
                            ModelError("模拟网络失败"))
        result = Agent(model, self.registry, self.store, trace_path=None).run("u", "w", "添加")
        self.assertEqual(result.status, "model_error")
        state = SessionStore(self.db).load("u", "w")
        self.assertEqual(state["todos"], [{"id": 1, "text": "应保存", "done": False}])
        self.assertEqual([m["tool_call_id"] for m in state["history"] if m["role"] == "tool"], ["a", "b"])

    def test_claims_release_on_exception_without_unbounded_lock_cache(self):
        for n in range(200):
            with self.assertRaises(RuntimeError):
                with self.store.claim("u", str(n)):
                    raise RuntimeError("中断")
            with SessionStore(self.db).claim("u", str(n)):
                pass
        self.assertFalse(SessionStore._active)

    def test_old_final_answer_is_not_displaced_by_tool_action_text(self):
        history = [{"role": "user", "content": "海鸥项目截止日期？"}]
        for n in range(8):
            history += [{"role": "assistant", "content": "准备查询。" * 80,
                         "tool_calls": [call("search", {"query": "海鸥"}, str(n))]},
                        {"role": "tool", "tool_call_id": str(n), "content": "结果"}]
        history.append({"role": "assistant", "content": "海鸥项目截止日期为十月十日。"})
        for n in range(6):
            history += [{"role": "user", "content": f"另一话题{n}"},
                        {"role": "assistant", "content": "好的"}]
        history.append({"role": "user", "content": "海鸥项目截止日期？"})
        self.state["history"] = history
        messages, _ = ContextBuilder().build(self.state, self.registry.definitions())
        self.assertIn("十月十日", messages[1]["content"])

    def test_http_response_duplicate_fields_are_protocol_errors(self):
        model = ChatModel("https://api.example.com", "TEST_KEY", "test")
        raw = b'{"choices":[],"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"ambiguous"}}]}'
        with patch("urllib.request.urlopen", return_value=io.BytesIO(raw)):
            with self.assertRaises(ModelError):
                model.complete([], [])

    def test_pagination_empty_last_page_and_invalid_bounds(self):
        result = self.registry.execute("todo", '{"action":"list","offset":100}', self.state)
        self.assertEqual(result["result"], {"items": [], "total": 0, "next_offset": None})
        for args in ({"action": "list", "offset": -1}, {"action": "list", "limit": 0},
                     {"action": "list", "limit": True}, {"action": "add", "text": "x", "offset": 1}):
            with self.subTest(args=args):
                self.assertFalse(self.registry.execute("todo", json.dumps(args), self.state)["ok"])
        self.assertEqual(self.state["todos"], [])


if __name__ == "__main__":
    unittest.main()
