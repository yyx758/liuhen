# 留痕（liuhen）后端

## 项目背景

留痕是面向个人的任务管理 AI Agent。用户用自然语言描述任务，Agent 负责理解时间、查询任务和发起操作；后端负责校验、持久化、并发控制与操作留痕。项目优先保证结果可靠、过程可追溯，并能通过测评复现和比较版本表现。

当前后端实现聚焦任务管理。Vue 前端位于 `frontend/app/`，已接入任务、对话、提案和审计 API。文档管理与检索、偏好和提醒仍是后续方向。

## 已实现的能力

| 模块 | 当前能力 |
| --- | --- |
| 任务 | 创建、查询；开始时间和截止时间独立，时长可选；有占用时段的任务参与时间冲突检查 |
| Agent | 多轮对话；调用创建、查询任务和读取提案工具；修改与删除只生成待确认提案 |
| 提案 | 独立的查询、确认、取消 API；前端可按当前待确认或会话读取提案；版本冲突、替换、过期和可判定执行失败有明确状态 |
| 可靠性 | Chat 请求与直接创建的幂等处理、同一会话并发保护、确认与取消的事务竞争 |
| 留痕 | 对话与工具过程持久化；业务操作写入只追加的审计日志 |
| 测评 | 21 条场景、隔离数据库、业务规则评分、LLM 回复裁判和人工抽样校准 |

## 项目架构

后端使用 Python、FastAPI、Pydantic 和 SQLAlchemy；开发时可连接 PostgreSQL，未配置时回退到 SQLite。模型请求通过 HTTP 发送，`.env` 使用 `python-dotenv` 读取。

```text
客户端 ── /chat ── chat_service ── agent_runner ── 模型接口
                              │              └─ ToolRegistry
                              │                       │
客户端 ── /tasks、/actions ────┴─────────────── 业务服务
                                                    │
                                                repositories
                                                    │
                                            PostgreSQL / SQLite

客户端 ── /audit/events、/audit/events/page、/audit/summary ── audit_service ── operation_events
```

`/chat` 的每一轮由 `chat_service` 加载历史、运行 Agent，并持久化消息和工具结果。Agent 工具在 `agent/tool_registry.py` 中显式注册；确认和取消提案只通过独立 API 执行。任务和提案 API 直接进入业务服务。服务层处理校验、事务和审计，仓储层负责数据库读写。

主要目录：

| 路径 | 内容 |
| --- | --- |
| `backend/app/api/` | FastAPI 路由 |
| `backend/app/agent/` | 模型调用、Agent 循环和工具注册 |
| `backend/app/services/` | 任务、提案、对话、幂等请求与审计的业务逻辑 |
| `backend/app/repositories/` | 任务和提案的数据访问 |
| `backend/app/models/`、`schemas/` | SQLAlchemy 表模型与 Pydantic 请求／响应模型 |
| `backend/app/db/`、`config/` | 数据库连接、时间与模型配置 |
| `backend/app/evals/`、`backend/tests/` | 真实模型测评和后端自动化测试 |

## 核心数据与操作规则

任务至少要有 `start_at`（开始时间）或 `due_at`（截止时间）。截止时间是期限，不由开始时间和时长计算；只有同时给出开始时间与 `duration_minutes` 时，才会计算日程结束时间 `end_at` 并检查时间段冲突。用户没有指定时长时，时长和日程结束时间保持为空。未带时区的任务时间按 UTC+08:00 解释。

Agent 可以直接创建任务；Agent 提议修改、删除时生成 `pending_actions`，保存任务版本与操作前后快照，用户通过 `POST /actions/{id}/confirm` 或 `/cancel` 作决定。同一条对话请求中的独立修改（如改标题和改时间）会生成可分别确认的提案；开始时间、结束时间和时长属于同一项排期修改。确认其中一项后，另一项会在任务未发生同字段冲突时基于最新版本执行。用户在页面手动修改、切换状态或删除任务时，调用 `PATCH /tasks/{id}` 或 `DELETE /tasks/{id}` 直接执行，不生成提案；请求须带当前版本的 `If-Match` 头，版本过旧返回 409。手动变更会将同一任务尚待确认的 Agent 提案标记为已替换。提案有效期为 10 分钟。新一轮请求的提案会替换同一任务尚待确认的旧提案；后台任务每 60 秒物理删除到期提案，确认时也即时检查有效期。`DELETE /actions/{id}` 可提前清理已过期提案，成功返回 204；未到期或已执行的提案返回 409，找不到时返回 404。

提案状态包括 `pending`、`executed`、`cancelled`、`expired`、`replaced`、`conflicted`、`failed`。确认与取消通过事务和条件状态转换争夺同一提案，只有一个请求能生效；确认还会核对任务版本。可判定的执行失败会回滚任务修改并记录 `failed`。手动修改、删除分别写入 `task_updated`、`task_deleted`，提案过期删除写入 `proposal_deleted`；这些事件与业务变更在同一事务中提交。

`POST /chat` 要求客户端为每个新意图生成 UUID `request_id`，重试同一意图时复用该值。已完成的重复请求返回先前结果；正在处理的重复请求返回 409。一个会话同一时间只处理一轮，进程中断后的请求可在租约到期后恢复。直接调用 `POST /tasks` 时，使用 UUID 格式的 `Idempotency-Key` 请求头；相同键与相同请求体复用首次创建结果，相同键配不同请求体返回 409。

`DELETE /chat/conversations/{conversation_id}` 删除会话消息和已完成的请求记录；正在处理的会话返回 409。删除后只保留已用请求 ID 的无内容标记，旧请求重试返回 410，不会再次执行工具。任务、提案和操作审计不随会话删除。

## 本地运行

在项目根目录运行，使用 Python 3.11 或更新版本。仓库目前没有依赖锁文件；以下命令安装当前代码用到的运行依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install fastapi uvicorn sqlalchemy pydantic httpx python-dotenv
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

在项目根目录的 `.env` 填写 `base_url`（完整的聊天接口地址）、`api_key` 和 `model`。Agent 与测评脚本都直接读取该文件；可选的 `judge_base_url`、`judge_api_key`、`judge_model` 用于独立裁判，未填写时沿用 Agent 模型配置。`.env` 已被 Git 忽略，不要提交真实密钥。

连接 PostgreSQL 时，先安装同步驱动 `python -m pip install "psycopg[binary]"`，并在 `.env` 增加 `database_url=postgresql+psycopg://用户名:URL编码后的密码@127.0.0.1:5432/数据库名`。不配置该项时使用本地 SQLite 文件。数据库密码包含 `@`、`/` 等字符时须进行 URL 编码。现有数据库需具备当前 ORM 定义的字段和约束；启动时不再升级旧表结构。

启动 API：

```powershell
python -m backend.app.main
```

服务默认监听 `http://127.0.0.1:8000`，交互式接口文档位于 `http://127.0.0.1:8000/docs`。本地 SQLite 文件是 `backend/liuhen_dev.db`；启动时会创建缺失表，并启动提案过期清理任务。PostgreSQL 写入测试可使用 `python -m backend.tests.postgres_smoke`，它创建并清理独立的临时测试库。

## API 概览

| 方法与路径 | 用途 |
| --- | --- |
| `POST /chat` | 发送一轮自然语言消息；请求包含 `request_id`、`user_text`，后续轮次带上 `conversation_id` |
| `DELETE /chat/conversations/{conversation_id}` | 删除会话与消息；处理中的会话不可删除，任务与审计保留 |
| `POST /tasks` | 直接创建任务；请求头包含 `Idempotency-Key` |
| `GET /tasks`、`GET /tasks/{task_id}` | 查询任务 |
| `PATCH /tasks/{task_id}`、`DELETE /tasks/{task_id}` | 手动直接修改、删除任务；请求头 `If-Match` 为当前任务版本 |
| `POST /tasks/{task_id}/proposals/update` | 生成修改提案 |
| `POST /tasks/{task_id}/proposals/delete` | 生成删除提案 |
| `GET /actions/{action_id}` | 查询提案及状态 |
| `POST /actions/{action_id}/confirm`、`/cancel` | 确认或取消提案 |
| `GET /audit/events` | 查询审计事件，支持 `task_id`、`action_id`、`before_id`、`limit` |

首次 Chat 请求示例：

```json
{
  "request_id": "c043f315-125c-48bf-aec7-9bcf3f36a02f",
  "user_text": "明天下午两点学习 Python"
}
```

响应包含 `conversation_id`，后续轮次传回该 ID。`/audit/events` 默认返回最近 50 条、最多 100 条；使用上一页最后一条的 `id` 作为 `before_id` 读取更早记录。

## 测评与人工校准

测评通过 `backend/app/evals/` 运行真实模型。每次运行使用独立的临时数据库，并可通过 `--now` 固定“明天”等相对时间的解释。业务准确性由程序给出 0/100 分；回复质量由 LLM 裁判给出 0/33/67/100 分。裁判失败时回复分为 `null`，报告会保留错误原因。

业务分按每题预期判定：普通创建题检查任务是否正确落库；故障注入题检查预期错误是否被正确处理、是否避免误创建或半修改。因此第 12、15 题在注入故障后安全失败，业务分仍可为 100；用户目标是否最终完成可以另设指标。回复分允许语义等价和上下文省略：多轮对话中用户已知的旧冲突、并发落败请求的 HTTP 码、以及无效输入后隐含的“尚未创建”，不因未重复表述而扣分；事实错误和误报成功仍为 0。

在项目根目录运行单题或全量测评（两条命令任选其一）：

```powershell
python -m backend.app.evals.run --case 01 --trials 3 --now 2026-11-18T10:00:00+08:00
python -m backend.app.evals.run --all --trials 3 --now 2026-11-18T10:00:00+08:00
```

全量包含 21 条场景；第 14 条每次覆盖三种确认／取消并发变体，因此运行 3 轮共得到 69 份记录。当前保留的全量报告是 `backend/evals/results/all.json`，再次运行会另存为 `all-2.json`。后续命令使用终端实际打印的报告路径；也可用 `--output` 指定路径。查看当前全量报告的汇总：

```powershell
$report = Get-Content backend/evals/results/all.json -Raw | ConvertFrom-Json
$report.summary
```

从已评分报告抽取约 20% 供人工盲审。相同报告与 `seed` 会选出相同记录；当前策略优先覆盖不同分档和复杂场景，再随机补齐：

```powershell
python -m backend.app.evals.calibration sample backend/evals/results/all.json --rate 0.2 --seed 42
```

在生成的 `backend/evals/reviews/all-review.json` 中，为每条记录填写 `human_business_score`（0/100）、`human_reply_score`（0/33/67/100）和 `human_reason`；可选填 `human_factual_error`、`human_forbidden_hit`。盲审文件不包含自动分数。完成后比较人工与自动评分：

```powershell
python -m backend.app.evals.calibration compare backend/evals/reviews/all-review.json
```

对比报告给出一致率、平均分差与严重漏判清单。调整裁判后，可对已有报告重新评分而不再次运行 Agent：

```powershell
python -m backend.app.evals.run --grade-report backend/evals/results/all.json
```

`backend/evals/results/` 和 `backend/evals/reviews/` 已被 Git 忽略。

## 自动化测试与当前范围

后端回归测试位于 `backend/tests/`，可从项目根目录运行：

```powershell
python -m unittest discover -s backend/tests -v
```

当前按本地个人项目实现，尚未加入账号与提案归属校验。生产对话自动转为测评候选案例的流程也尚未实现；目前的 21 条测评场景由人工维护。
