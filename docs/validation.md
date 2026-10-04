# 提交副本验证记录

日期：2026-10-05。桌面清洁副本已独立重新安装并运行测试，不依赖原目录的虚拟环境。

| 项目 | 实际结果 |
| --- | --- |
| 环境 | Windows，桌面副本 Python 3.12.7；原目录 Python 3.12.14 |
| 全新安装 | uv sync --frozen 成功，锁文件未修改 |
| 自动测试 | 桌面副本 41 项通过，1.701 秒；原目录同日 41 项通过 |
| 额外数据 | 500 次固定种子随机 todo CRUD，100 条含转义字符待办完整分页；已计入 41 项 |
| CLI | --help 入口可运行 |
| API 配置 | 已按 DeepSeek 官方说明准备 deepseek-flash 与非思考模式 |
| 真实 API | 尚未执行，等待本地密钥；不能记为通过 |
| GitHub | 公开仓库 https://github.com/llwll999/guangchen-agent-harness；本文记录本地安装和离线验证，云端运行结果以 Actions 页面为准 |
| 云端 CI | GitHub Actions 成功：Ubuntu/Python 3.11、Windows/Python 3.12；运行 37224556196 |

离线运行原始输出见 offline-test-output.txt；它仅包含测试名称和结果。离线模型脚本测试不证明真实模型能自主选择工具。

真实验收使用 scripts/smoke_real.py，执行六个用户任务，检查聊天追问、calculator 结果用于 todo、完成待办、窗口隔离和 search 调用。scripts/configure_and_smoke.py 只在本地终端遮蔽输入密钥，生成不含密钥的 docs/real-api-validation.json。这个文件只有真实执行后才存在；失败状态不得改写为 passed。

已验证边界包括严格 JSON、Schema、工具失败本地回滚、Schema/结果引用隔离、上下文预算及同进程多 store 排他。尚未实现跨进程并发、异步 inbox、语义记忆、精确 token 预算、外部工具业务幂等和思考模式适配。详见 review-report.md 与 README。

## GitHub 发布后的复核

提交 809543a055cb6a7484151b5c6d268f13b4acea42 的 26 个文件与桌面版本文本内容一致。远端克隆后 uv sync --frozen 成功，41 项测试通过（0.770 秒），CLI 帮助入口正常。

云端运行：https://github.com/llwll999/guangchen-agent-harness/actions/runs/37224556196。两种环境的任务均 completed / success。后续文档更新的检查结果以 Actions 页面最新状态为准。

用户反馈在 CMD 中填写了模型和密钥，但本地 docs/real-api-validation.json 尚未生成；真实 API 验收状态仍待确认，不把输入配置当成验收通过。
