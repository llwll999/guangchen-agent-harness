# 代码阅读和面试练习

先读 README，再按 `runtime → model → tools → store → context → cli` 阅读。每看到一行都回答三个问题：它读取什么，改变什么，失败后发生什么。不要把这份讲解当成已经掌握代码的证明。

## 1. 主循环：mini_agent/runtime.py

`RunResult` 用 dataclass 定义回答、状态和本轮 trace；它是程序内部结果，不是模型消息。`Agent.__init__` 注入模型、工具和存储，因此测试可替换模型而保留同一套主循环。

`run` 在写入前校验用户/session ID 和输入长度。`with store.claim` 表示运行期间独占当前会话；返回、异常都会进入上下文管理器的 `finally` 释放锁。

`_run` 读取独立状态，追加 user 消息。`run_id` 标识本次执行，`session_hash` 方便关联同一窗口又不直接打印用户名字。内部 `event` 闭包把日志放到本轮列表，记录 UTC 时间和元数据。

`seen_calls` 是本轮已执行调用 ID 的集合；它不检查参数是否相同，也不负责跨任务去重。模型使用同一个 ID 请求第二次写入时停止，不能把两次写入都执行。

`for step ...` 限制模型决策次数。每轮先构造 context，再请求模型。模型失败和输入超预算不是工具失败：它们会终止循环，不假装工具成功。

拿到响应后，先检查重复 ID，再追加完整 assistant 消息。没有 `tool_calls` 才将 content 作为最终答案；有调用时，即便 content 写了“执行工具”，也继续。

每个 call 读取 function 名称和 JSON 参数交给 registry。执行后必须追加带同一 `tool_call_id` 的 tool 消息，不能只把结果拼成普通聊天文本。结果太大时生成明确标记的预览；状态中的原始 todo 不变。耗时用单调时钟 `monotonic`，不会受系统调时影响。

循环停止但没拿到最终答案时，Runtime 自己追加一个说明消息。随后将历史和 todo 同时存入 SQLite，最后追加 JSONL 日志。日志写失败只增加一个 trace 事件，不改变已经保存的业务状态。

练习：关闭文件，写出循环伪代码；指出 assistant 调用和 tool 结果各在哪里追加；解释为什么最后一步仍可执行工具但返回 `max_steps`。

## 2. 模型适配：mini_agent/model.py

`ModelError` 区分可以展示的模型/网络错误与程序内部 bug。`parse_message` 先确认 choices 中有完整 assistant，再核对 finish_reason。`length` 表示被截断，不能当完整 JSON/最终答案使用。

解析器确认 content 类型、工具列表和单步调用数量，逐个校验 ID、名称、参数字符串；同一个批次重复 ID 直接拒绝整个响应，所以不会先执行第一条再发现后面的结构错误。工具参数“是否为合法 JSON、是否满足 Schema”留给 registry，从而成为模型可修正的工具错误。

只复制协议允许的字段，避免把提供商额外对象原样塞进上下文。当前适配器遇到 reasoning_content 会明确拒绝。面试时不要声称它支持所有 OpenAI-compatible 服务商的全部模型。

`from_env` 读取配置，不读取聊天工具本身的凭据。`complete` 把 Unicode JSON 编成 UTF-8，POST 到 API，tools 带上注册 Schema。非流式实现容易检查完整消息；代价是用户看不到 first token 流。

HTTP 429 和 5xx 最多重试一次，超时和其他错误不无限重试。只重试模型请求，没有重新执行已执行过的工具。错误不打印服务商返回体，以免回显用户文本。

练习：给调用 ID c1 手写 assistant 和对应 tool 两条 JSON；解释 Schema 出现在请求里为什么仍要执行端校验。

## 3. 工具系统：mini_agent/tools.py

`Tool` 包含名称、描述、参数 Schema 和函数。frozen dataclass 限制属性重新赋值，但内部 dict 仍可变。注册和 `definitions` 两个边界都深拷贝 Schema；否则调用方修改参数定义，就可能改变执行端校验。注册也校验名称、Schema 和重名。

`execute` 先查工具，用 `jsonutil.strict_loads` 解 JSON，再校验 Schema。严格解析拒绝重复键、NaN、Infinity 和溢出到无穷大的数字。handler 在 state 副本执行，验证结果和状态可编码后才提交；失败返回统一结构并丢弃副本。返回结果单独复制，防止对象引用泄漏。副本提交只保护本地内存，无法回滚外部副作用。

todo 的动作相关要求仍在 handler 检查。list 返回结构化分页；序列化长度检查会包含引号、反斜杠、控制字符转义及分页元信息。`next_offset` 为 None 表示结束，非空则需要下一页。模型仍受步数预算约束，一次任务不保证能读完 100 条。

`calculator` 使用 ast.parse 构建语法树，递归函数只接受数字节点、白名单二元运算和正负号。`type(value) in (int, float)` 排除了 Python 的 bool（bool 是 int 子类）。限制节点数和数值，禁止函数调用、属性访问、下标、乘方等。

`search` 是 mock；匹配关键词的是这个具体检索工具，不是 Agent 的决策路由。`todo` 直接改当前 session 的内存 state，ID 单调增加，因此删除再新增不复用旧 ID。

练习：加入一个 read_docs 工具，只改注册和 handler；指出为什么 `eval` 不合适；对每种 todo 动作说出缺参数时的结果。

## 4. 存储：mini_agent/store.py

组合主键同时包含用户和窗口；SQL 的 `?` 参数化避免把 ID 当 SQL 代码。每次 load 都反序列化成独立对象，避免多个 session 共用同一个 list。

类级 `_guard` 保护活动集合的检查和插入；key 包含规范化数据库路径、user 和 session。这样即使创建两个 store 对象，也不能同时进入同一会话。guard 只覆盖短操作，整个网络等待期间由活动标记表达会话所有权；退出时 finally 删除标记。它也避免为所有历史 session 长期缓存锁。

`closing(connection)` 负责关闭连接，后面的 `db` 上下文负责提交或回滚；SQLite connection 自己的 with **不会关闭连接**。这个区别在 Windows 文件占用测试中确实暴露过问题。

SQLite 的短事务负责最终原子保存，活动标记负责长任务互斥，它们解决不同层级问题。同进程多个 store 已覆盖；两个 CLI 进程同时写一个数据库仍不受此标记保护。直接绕过 claim 调用 load/save 也不受长任务互斥保护。

练习：解释网络等待为何不放在数据库写事务中；说清进程崩溃后哪部分 state 保留。

## 5. 上下文：mini_agent/context.py

`turn_groups` 以 user 消息开始新组，组内保留 assistant 调用、全部 tool 结果和最终答案。以消息条数随意取尾部可能制造没有调用来源的 tool 结果。

`excerpt` 是截取式摘要，给用户问题和最终答案分别留空间，工具名称放后面。`turn_keywords` 对完整会话文本检索，`window` 把查询命中处放进有限摘录，修复长回答末尾事实被前缀裁剪的问题。每次 build 的打分只算一次，预算循环复用分数。它仍会漏同义表达、匹配无关内容或选错多处命中。

`build` 每次构造模型输入；旧历史不从数据库删除。摘要、关键词召回、todo 预览是信息线索，system 则是固定行为规则。资料以 user 数据消息进入，实际最近历史按原顺序跟在后面。

每次尝试都计算序列化长度，包括工具定义。不够时移走较旧的一整个用户轮次；当前轮次不可拆分，不够就停止。预算是字符预算，不能声称等于某个模型的精确 token 数。

练习：第 21 轮有“海鸥项目截止日期”，第 201 轮问截止日期时，分别指出原文在哪、怎么命中、放进哪条消息。然后把“海鸥”改成“之前那个项目”，说明为何可能召回失败。

## 6. CLI 与测试

CLI 的 `/session` 切换命名空间，`/todos` 是本地查看命令，不调用模型。`--message` 方便脚本验收并用退出码反馈失败。Ctrl+C 会结束并丢弃当前未提交任务。

测试 `ScriptModel` 提供已知响应来检验主循环，它不证明模型真的能从 Schema 选择工具。真实测试单独使用 ChatModel，检查数据库和工具 trace，而不仅判断最终答案写得像成功。

## 面试前自测

1. 不看实现，十分钟写出工具循环和两条消息配对。
2. 解释没有框架时谁决定动作、谁执行动作、谁保存结果。
3. 演示同一用户两个窗口，重启后分别恢复。
4. 输入 `1/0` 和无效工具参数，说出错误如何进入下一轮。
5. 解释“最多八步”与“最多八个调用/步”的区别。
6. 说出基础压缩的损失、召回失败的反例，以及精确待办为什么仍可查。
7. 说清哪些已通过自动测试，哪些必须真实模型验证。
8. 在存储崩溃、模型超时和跨进程并发三种情况中，分别描述状态是否可信。

答不出来的函数应重新阅读或简化。最终提交只保留自己能讲明白的代码。

补充阅读：`docs/review-report.md` 有实际失败样本，`docs/interview-qa.md` 有逐题追问、回答边界和练习。重点手写三个函数：`strict_loads` 的重复键检查、`bounded_observation` 的二分预算、`claim` 的活动标记生命周期。
