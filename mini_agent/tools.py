"""Tool schemas, validation and bounded local implementations."""

import ast
import json
import math
import operator
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable

from jsonschema import Draft202012Validator, ValidationError

from .jsonutil import strict_loads


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[[dict, dict], object]


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        if not isinstance(tool.name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool.name):
            raise ValueError("工具名应为 1–64 个字母、数字、下划线或连字符。")
        if tool.name in self._tools:
            raise ValueError(f"重复工具名：{tool.name}")
        Draft202012Validator.check_schema(tool.schema)
        self._tools[tool.name] = Tool(tool.name, tool.description, deepcopy(tool.schema), tool.handler)

    def definitions(self) -> list[dict]:
        return [{"type": "function", "function": {
            "name": t.name, "description": t.description, "parameters": deepcopy(t.schema),
        }} for t in self._tools.values()]

    def execute(self, name: str, raw: str, state: dict) -> dict:
        if name not in self._tools:
            return {"ok": False, "error": "unknown_tool"}
        tool = self._tools[name]
        try:
            if not isinstance(raw, str) or len(raw) > 4000:
                raise ValueError("参数过长或不是字符串。")
            args = strict_loads(raw)
            if not isinstance(args, dict):
                raise ValueError("参数不是对象。")
            Draft202012Validator(tool.schema).validate(args)
        except (ValueError, TypeError, ValidationError, RecursionError):
            return {"ok": False, "error": "invalid_arguments",
                    "hint": "参数必须是符合工具 Schema 的 JSON 对象。"}
        try:
            # Local transaction: commit only after both state and result are JSON.
            # This does not roll back an external HTTP/file side effect.
            working = deepcopy(state)
            result = tool.handler(args, working)
            json.dumps(result, allow_nan=False)
            json.dumps(working, allow_nan=False)
            committed = deepcopy(working)
            observation = {"ok": True, "result": deepcopy(result)}
            state.clear()
            state.update(committed)
            return observation
        except (ValueError, ArithmeticError, SyntaxError, RecursionError) as exc:
            return {"ok": False, "error": "tool_failed", "hint": str(exc)[:200]}
        except Exception:
            # Do not put internal exceptions or secrets in model context.
            return {"ok": False, "error": "internal_tool_error"}


def calculator(args: dict, state: dict):
    """AST allowlist: no eval, names, attributes, calls or power operator."""
    tree = ast.parse(args["expression"], mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 64:
        raise ValueError("表达式过于复杂。")
    ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.Mod: operator.mod}

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = node.value
        elif isinstance(node, ast.BinOp) and type(node.op) in ops:
            value = ops[type(node.op)](visit(node.left), visit(node.right))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        else:
            raise ValueError("仅支持数字、括号及 + - * / %。")
        if not math.isfinite(value) or abs(value) > 1e12:
            raise ValueError("数值超出范围。")
        return value

    return {"value": visit(tree.body)}


SEARCH_DOCS = [
    {"id": "agent-loop", "title": "Agent 循环", "text": "模型选择工具，Runtime 执行并回传结果，直到最终回答。"},
    {"id": "session", "title": "Session 隔离", "text": "用户和窗口共同标识会话，对话与待办分别持久化。"},
    {"id": "context", "title": "Context 压缩", "text": "保留近期完整轮次，将旧历史摘要和相关记忆加入上下文。"},
]


def search(args: dict, state: dict):
    query = args["query"].casefold()
    words = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", query)
    ranked = sorted(SEARCH_DOCS, key=lambda d: sum(
        w in (d["title"] + d["text"]).casefold() for w in words), reverse=True)
    results = [d for d in ranked if any(
        w in (d["title"] + d["text"]).casefold() for w in words)]
    return {"mock": True, "source": "本地三条演示文档，非互联网搜索", "items": results[:3]}


def todo(args: dict, state: dict):
    action = args["action"]
    items = state["todos"]
    if action == "list":
        offset = args.get("offset", 0)
        limit = args.get("limit", 10)
        page = {"items": [], "total": len(items), "next_offset": None}
        # Budget the serialized result too, including escaping and metadata.
        for item in items[offset:offset + limit]:
            candidate = {**page, "items": page["items"] + [item],
                         "next_offset": offset + len(page["items"]) + 1}
            if len(json.dumps({"ok": True, "result": candidate}, ensure_ascii=False)) > 2800:
                break
            page["items"].append(item)
        end = offset + len(page["items"])
        page["next_offset"] = end if end < len(items) else None
        if not page["items"] and offset < len(items):
            raise ValueError("单条待办超过分页预算。")
        return page
    if "offset" in args or "limit" in args:
        raise ValueError("offset/limit 仅用于 list。")
    if action == "add":
        text = args.get("text", "").strip()
        if not text:
            raise ValueError("add 操作必须提供非空 text。")
        if len(items) >= 100:
            raise ValueError("演示版每个会话最多保留 100 条待办。")
        item = {"id": state["next_todo_id"], "text": text, "done": False}
        state["next_todo_id"] += 1
        items.append(item)
        return item
    if "id" not in args:
        raise ValueError("complete/delete 操作必须提供 id。")
    item = next((item for item in items if item["id"] == args["id"]), None)
    if item is None:
        raise ValueError("待办不存在。")
    if action == "complete":
        item["done"] = True
    else:
        items.remove(item)
    return item


def default_registry() -> ToolRegistry:
    registry = ToolRegistry()

    def schema(properties, required):
        return {"type": "object", "properties": properties,
                "required": required, "additionalProperties": False}

    registry.register(Tool("calculator", "计算有限长度的算术表达式。", schema({
        "expression": {"type": "string", "minLength": 1, "maxLength": 160},
    }, ["expression"]), calculator))
    registry.register(Tool("search", "搜索本地 mock 文档，不提供实时事实。", schema({
        "query": {"type": "string", "minLength": 1, "maxLength": 200},
    }, ["query"]), search))
    registry.register(Tool("todo", "管理当前会话待办。add 需 text；complete/delete 需 id。"
                           "list 分页返回 items/total/next_offset；非空 next_offset 可用于下一页。", schema({
        "action": {"type": "string", "enum": ["add", "list", "complete", "delete"]},
        "text": {"type": "string", "minLength": 1, "maxLength": 200},
        "id": {"type": "integer", "minimum": 1},
        "offset": {"type": "integer", "minimum": 0, "maximum": 100},
        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
    }, ["action"]), todo))
    return registry
