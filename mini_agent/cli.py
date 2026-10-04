"""Interactive local CLI with independently resumable sessions."""

import argparse
import json

from .model import ChatModel
from .runtime import Agent
from .store import SessionBusy, SessionStore
from .tools import default_registry


def main():
    parser = argparse.ArgumentParser(description="Minimal agent, real LLM required")
    parser.add_argument("--user", default="local")
    parser.add_argument("--session", default="window-1")
    parser.add_argument("--db", default="data/sessions.db")
    parser.add_argument("--trace", default="data/trace.jsonl")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--message", help="运行一个任务后退出；失败状态返回非零退出码")
    args = parser.parse_args()
    try:
        model = ChatModel.from_env()
        store = SessionStore(args.db)
        agent = Agent(model, default_registry(), store,
                      max_steps=args.max_steps, trace_path=args.trace)
    except ValueError as exc:
        parser.exit(2, str(exc) + "\n")

    def send(text):
        result = agent.run(args.user, args.session, text)
        for event in result.trace:
            if event["event"] == "tool":
                print(f"[tool] {event['tool']} ok={event['ok']} {event['duration_ms']}ms")
        print(f"[{result.status}] {result.answer}")
        return result.status == "completed"

    if args.message:
        try:
            success = send(args.message)
        except (ValueError, SessionBusy) as exc:
            parser.exit(2, str(exc) + "\n")
        parser.exit(0 if success else 1)
    print("命令：/session 名称，/todos，/exit。相同名称可恢复此前会话。")
    while True:
        try:
            text = input(f"{args.user}/{args.session}> ").strip()
            if text == "/exit":
                break
            if text.startswith("/session "):
                session = text[len("/session "):].strip()
                if not session or len(session) > 100:
                    print("会话名称应为 1–100 字符。")
                else:
                    args.session = session
                continue
            if text == "/todos":
                print(json.dumps(store.load(args.user, args.session)["todos"], ensure_ascii=False, indent=2))
                continue
            if text:
                send(text)
        except (ValueError, SessionBusy) as exc:
            print(str(exc))
        except (EOFError, KeyboardInterrupt):
            print("\n退出。正在执行时中断会丢弃当前尚未提交的本地任务。")
            break


if __name__ == "__main__":
    main()
