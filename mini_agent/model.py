"""Non-streaming Chat Completions HTTP adapter and strict envelope parser."""

import json
import os
import time
import urllib.error
import urllib.request

from .jsonutil import strict_loads


class ModelError(Exception):
    pass


def parse_message(payload: dict) -> dict:
    try:
        choice = payload["choices"][0]
        message = choice["message"]
        reason = choice["finish_reason"]
        if reason not in ("stop", "tool_calls") or message.get("role") != "assistant":
            raise ValueError("非完整 assistant 响应")
        # This small adapter supports non-thinking models only. Provider
        # reasoning protocols require a separate adapter, not silently dropping fields.
        if message.get("reasoning_content"):
            raise ValueError("请使用非思考模式；此适配器不支持 reasoning_content")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError("content 不是文本")
        if content and len(content) > 16000:
            raise ValueError("content 太长")
        raw_calls = message.get("tool_calls")
        calls = [] if raw_calls is None else raw_calls
        if not isinstance(calls, list) or len(calls) > 8:
            raise ValueError("单步最多 8 个工具调用")
        if bool(calls) != (reason == "tool_calls"):
            raise ValueError("结束原因和调用不一致")
        ids = set()
        parsed = {"role": "assistant", "content": content}
        clean_calls = []
        for call in calls:
            call_id = call["id"]
            fn = call["function"]
            if (call["type"] != "function" or not isinstance(call_id, str)
                    or not call_id or len(call_id) > 200 or call_id in ids
                    or not isinstance(fn["name"], str) or not fn["name"]
                    or not isinstance(fn["arguments"], str) or len(fn["arguments"]) > 4000):
                raise ValueError("工具调用结构无效")
            ids.add(call_id)
            clean_calls.append({"id": call_id, "type": "function", "function": {
                "name": fn["name"], "arguments": fn["arguments"],
            }})
        if clean_calls:
            parsed["tool_calls"] = clean_calls
        elif not content or not content.strip():
            raise ValueError("空最终回答")
        return parsed
    except (KeyError, IndexError, TypeError, AttributeError, ValueError) as exc:
        raise ModelError(f"模型响应协议错误：{exc}") from exc


class ChatModel:
    def __init__(self, base_url: str, api_key: str, model: str, thinking_disabled=False):
        if not base_url or not api_key or not model:
            raise ValueError("请设置 LLM_BASE_URL、LLM_API_KEY、LLM_MODEL。")
        if not base_url.startswith("https://"):
            raise ValueError("LLM_BASE_URL 必须使用 https。")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.thinking_disabled = thinking_disabled

    @classmethod
    def from_env(cls):
        return cls(os.getenv("LLM_BASE_URL", ""), os.getenv("LLM_API_KEY", ""),
                   os.getenv("LLM_MODEL", ""), os.getenv("LLM_THINKING") == "disabled")

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        body = {"model": self.model, "messages": messages, "tools": tools,
                "tool_choice": "auto", "stream": False, "max_tokens": 2048}
        if self.thinking_disabled:
            body["thinking"] = {"type": "disabled"}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.url, data=data, headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
        })
        for attempt in range(2):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ModelError("模型响应超过 1 MB。")
                return parse_message(strict_loads(raw))
            except urllib.error.HTTPError as exc:
                status = exc.code
                exc.close()
                if attempt == 0 and (status == 429 or 500 <= status < 600):
                    time.sleep(0.25)
                    continue
                # Provider response bodies sometimes echo prompts. Keep them out of logs.
                raise ModelError(f"LLM HTTP {status}；请检查配置或稍后重试。") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise ModelError("LLM 网络请求失败或超时。") from exc
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise ModelError("LLM 返回了无效 JSON。") from exc
