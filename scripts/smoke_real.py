"""Opt-in real API acceptance. Uses a disposable database, never a fake model."""

import tempfile
from pathlib import Path

from mini_agent.model import ChatModel
from mini_agent.runtime import Agent
from mini_agent.store import SessionStore
from mini_agent.tools import default_registry


def main():
    model = ChatModel.from_env()
    with tempfile.TemporaryDirectory() as directory:
        store = SessionStore(str(Path(directory) / "smoke.db"))
        agent = Agent(model, default_registry(), store, trace_path=None)

        def run(session, text):
            result = agent.run("smoke-user", session, text)
            print(f"[{session}/{result.status}] {result.answer}")
            assert result.status == "completed", result.status
            return result

        run("chat", "我的名字叫小林。请简短回答。")
        answer = run("chat", "我叫什么名字？不要调用工具。")
        assert "小林" in answer.answer, "纯聊天追问未能保留名字"
        first = run("window-1", "请用 calculator 计算 6*7，随后将计算结果加入 todo，text 格式为 联调结果=<结果>。")
        used = {e["tool"] for e in first.trace if e["event"] == "tool" and e["ok"]}
        assert {"calculator", "todo"} <= used, "未真实调用计算和待办工具"
        items = store.load("smoke-user", "window-1")["todos"]
        assert len(items) == 1 and "42" in items[0]["text"], "工具结果没有用于待办"
        run("window-1", "完成刚才添加的那条待办。")
        assert store.load("smoke-user", "window-1")["todos"][0]["done"]
        run("window-2", "请使用 todo 列出这个窗口的待办。")
        assert store.load("smoke-user", "window-2")["todos"] == []
        search = run("window-2", "使用 search 查找关于 session 的本地文档。")
        assert any(e.get("tool") == "search" and e["ok"] for e in search.trace)
    print("真实模型烟雾测试通过：聊天追问、多步工具、工具追问、session 隔离、search。")


if __name__ == "__main__":
    main()
