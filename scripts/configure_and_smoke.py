"""Prompt locally for API settings; keep the key out of files and command history."""

import getpass
import json
import os
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

from smoke_real import main as smoke


def main():
    if not sys.stdin.isatty():
        raise SystemExit("请在本地交互终端运行此脚本，以便遮蔽密钥输入。")
    base = input("API 根地址 [https://api.deepseek.com]：").strip() or "https://api.deepseek.com"
    model = input("模型名称 [deepseek-flash]：").strip() or "deepseek-flash"
    if not base.startswith("https://") or not model:
        raise SystemExit("请填写 HTTPS API 根地址和模型名称。")
    thinking = input("禁用思考模式 [Y/n]（本项目使用非思考模式）：").strip().lower() or "y"
    # Refuse getpass's plaintext fallback when echo cannot be disabled.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        key = getpass.getpass("API key（输入隐藏，不保存）：")
    if not key:
        raise SystemExit("API key 不能为空。")
    names = ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_THINKING")
    prior = {name: os.environ.get(name) for name in names}
    report = {"date": datetime.now(timezone.utc).isoformat(), "model": model,
              "status": "failed", "tasks": 6}
    try:
        os.environ.update(LLM_BASE_URL=base, LLM_MODEL=model, LLM_API_KEY=key)
        if thinking == "y":
            os.environ["LLM_THINKING"] = "disabled"
        else:
            os.environ.pop("LLM_THINKING", None)
        smoke()
        report["status"] = "passed"
    finally:
        for name, value in prior.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        key = ""
        target = Path(__file__).resolve().parents[1] / "docs" / "real-api-validation.json"
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("验收状态已保存；不包含密钥、响应正文或服务商错误内容。")


if __name__ == "__main__":
    try:
        main()
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        raise SystemExit("输入已取消；请在本地交互终端重新运行。") from None
