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
| 真实 API | 2026-10-05 11:55（上海时间），用户本地 deepseek-flash 非思考模式，六个任务验收通过；首次 401 失败见下方历史记录 |
| GitHub | 公开仓库 https://github.com/llwll999/guangchen-agent-harness；本文记录本地安装和离线验证，云端运行结果以 Actions 页面为准 |
| 云端 CI | GitHub Actions 成功：Ubuntu/Python 3.11、Windows/Python 3.12；运行 37224556196 |

离线运行原始输出见 offline-test-output.txt；它仅包含测试名称和结果。离线模型脚本测试不证明真实模型能自主选择工具。

真实验收使用 scripts/smoke_real.py，执行六个用户任务，检查聊天追问、calculator 结果用于 todo、完成待办、窗口隔离和 search 调用。scripts/configure_and_smoke.py 只在本地终端遮蔽输入密钥，生成不含密钥的 docs/real-api-validation.json。这个文件只有真实执行后才存在；失败状态不得改写为 passed。

已验证边界包括严格 JSON、Schema、工具失败本地回滚、Schema/结果引用隔离、上下文预算及同进程多 store 排他。尚未实现跨进程并发、异步 inbox、语义记忆、精确 token 预算、外部工具业务幂等和思考模式适配。详见 review-report.md 与 README。

## GitHub 发布后的复核

提交 809543a055cb6a7484151b5c6d268f13b4acea42 的 26 个文件与桌面版本文本内容一致。远端克隆后 uv sync --frozen 成功，41 项测试通过（0.770 秒），CLI 帮助入口正常。

云端运行：https://github.com/llwll999/guangchen-agent-harness/actions/runs/37224556196。两种环境的任务均 completed / success。后续文档更新的检查结果以 Actions 页面最新状态为准。

2026-10-05 03:23:30（上海时间）本地验收报告已生成，status=failed，模型误填为 deepsekk-flash。截图确认第一个聊天任务即返回 HTTP 401，不能把报告 tasks=6 解释为完成六个任务；旧字段表示计划任务数。模型拼写错误与密钥认证问题需分别修正。

## 密钥输入体验修复（2026-10-05）

Windows 输入改为星号遮蔽，并显示实际收到的字符数；支持退格与 Ctrl+U 清空。首次 API 任务失败时退出码为 1，保存 failed 报告并恢复原环境，不再打印 AssertionError 堆栈。新报告用 planned_tasks 表示计划数量；本地报告不纳入 Git 或源码包。

新增 5 项离线测试，连同原 41 项共 46 项通过（0.785 秒）。另在 Windows 交互终端用测试占位值验证粘贴：显示 15 个星号和 15 字符提示，无明文输出，未调用 API。以上输入测试不代表真实密钥认证；之后的真实验收结果见下一节。

## 真实 API 验收通过（2026-10-05）

本次由用户在本地终端输入密钥并运行，AI 未接收或保存密钥。用户截图显示全部任务 completed，末尾打印真实模型烟雾测试通过。本地报告 date=2026-10-05T03:55:12.454520+00:00（上海时间 11:55:12），model=deepseek-flash，status=passed，planned_tasks=6。运行版本对应输入修复提交 f0262be8b63c0eb879bf70343dfed6512041a5dc；本次仅更新文档，源码逻辑未修改。

实际检查：姓名输入和追问；calculator 计算 6*7 后使用结果添加 todo；完成刚才的 todo；另一个窗口待办为空；search 确实调用本地 mock 文档。计算任务还检查成功调用 calculator 与 todo，以及数据库恰好一条待办且含 42，后续检查其 done 状态。

模型输出同时出现了无依据的重复待办警告。通过的数据库断言说明添加后实际只有一条，不能把这段措辞当作发生重复写入的证据。源码把当前 todo_preview 与旧对话资料一起放在标为历史资料的消息中，可能让模型误把当前状态当成另一条历史记录；这是解释上的局限，尚未实测提示改进。该烟雾测试没有检查所有自然语言表述，不能据一次通过宣称模型不会幻觉或任意任务都成功。

本地 JSON 报告不上传，提交记录仅公开模型、时间、场景及结果，不含密钥。
