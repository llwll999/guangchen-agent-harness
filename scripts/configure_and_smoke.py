"""Prompt locally for API settings; keep the key out of files and command history."""

import getpass
import json
import os
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

from smoke_real import main as smoke

REPORT_PATH = Path(__file__).resolve().parents[1] / "docs" / "real-api-validation.json"


def read_masked_key(read_char, write):
    """Read Windows console characters; echo only masks, never the secret."""
    chars = []
    while True:
        char = read_char()
        if not char:
            raise EOFError
        if char in ("\r", "\n"):
            write("\n")
            return "".join(chars)
        if char == "\x03":
            raise KeyboardInterrupt
        if char in ("\x00", "\xe0"):
            read_char()  # Windows extended-key prefix and key code.
        elif char in ("\b", "\x7f"):
            if chars:
                chars.pop()
                write("\b \b")
        elif char == "\x15":  # Ctrl+U clears a mistaken paste.
            write("\b \b" * len(chars))
            chars.clear()
        elif char.isprintable():
            chars.append(char)
            write("*")


def prompt_key():
    print("请粘贴完整密钥（Windows Terminal：Ctrl+Shift+V），再按回车。")
    print("只显示星号；退格可删除，Ctrl+U 可清空。密钥不保存到文件。")
    if os.name == "nt":
        import msvcrt

        def write(text):
            sys.stdout.write(text)
            sys.stdout.flush()

        write("API key：")
        key = read_masked_key(msvcrt.getwch, write)
    else:
        # Python 3.11/3.12 getpass has no echo_char; refuse plaintext fallback.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass("API key（字符不显示）：")
    key = key.strip()
    if not key:
        raise SystemExit("未收到密钥，请重新运行并粘贴。")
    if any(char.isspace() for char in key) or key.startswith(("\"", "'")):
        raise SystemExit("密钥含空白或引号，请只复制密钥本身。")
    print(f"已收到 {len(key)} 个字符；字符数只能确认粘贴成功，是否有效以 API 验证为准。")
    return key


def main():
    if not sys.stdin.isatty():
        raise SystemExit("请在本地交互终端运行此脚本，以便遮蔽密钥输入。")
    print("使用官方 DeepSeek Flash 时，下面三项直接按回车即可。")
    base = input("API 根地址 [https://api.deepseek.com]：").strip() or "https://api.deepseek.com"
    model = input("模型名称 [deepseek-flash]：").strip() or "deepseek-flash"
    if not base.startswith("https://") or not model:
        raise SystemExit("请填写 HTTPS API 根地址和模型名称。")
    if model == "deepsekk-flash":
        raise SystemExit("模型名多了一个 k：请重新运行，模型项直接回车使用 deepseek-flash。")
    thinking = input("禁用思考模式 [Y/n]（本项目使用非思考模式）：").strip().lower() or "y"
    key = prompt_key()
    names = ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_THINKING")
    prior = {name: os.environ.get(name) for name in names}
    report = {"date": datetime.now(timezone.utc).isoformat(), "model": model,
              "status": "failed", "planned_tasks": 6}
    try:
        os.environ.update(LLM_BASE_URL=base, LLM_MODEL=model, LLM_API_KEY=key)
        if thinking == "y":
            os.environ["LLM_THINKING"] = "disabled"
        else:
            os.environ.pop("LLM_THINKING", None)
        smoke()
        report["status"] = "passed"
    except AssertionError:
        print("验收未通过，请看上方的任务状态。")
        print("若显示 HTTP 401：密钥认证失败，请重新复制该 API 服务商的有效密钥。")
        print("官方 DeepSeek 密钥在 https://platform.deepseek.com/api_keys 管理；不要复制网页登录密码。")
        return 1
    finally:
        for name, value in prior.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        key = ""
        REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("验收状态已保存；不包含密钥、响应正文或服务商错误内容。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        raise SystemExit("输入已取消；请在本地交互终端重新运行。") from None
