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
| 测评 | 26 条场景（含跨用户隔离）、隔离数据库、固定测评时间点、业务规则评分、LLM 回复裁判和人工抽样校准 |

## 项目架构

后端使用 Python、FastAPI、Pydantic、SQLAlchemy 和 PostgreSQL。模型请求通过 HTTP 发送，`.env` 使用 `python-dotenv` 读取。

账号与数据归属：`users` 表包含唯一用户名、可选唯一邮箱、密码哈希、角色、状态（`pending`/`active`/`rejected`/`disabled`）、审核信息和创建／更新时间。任务、提案、会话、对话轮次、幂等记录和操作留痕都带 `user_id`，每个用户只能读写自己的数据；他人的任务、提案、会话按不存在处理（404），他人用过的 `request_id`、`Idempotency-Key` 返回 409 且不回放内容；待办时间冲突只在同一用户内判断。当前用户来自登录会话，见下文"登录与账号"。

### 登录与账号

- 密码只保存 Argon2id 哈希。登录成功后在 `user_sessions` 记录会话，浏览器拿到 HttpOnly、SameSite=Lax 的 `liuhen_session` Cookie；数据库只存令牌的 SHA-256。勾选"保持登录"时会话 7 天有效，否则 12 小时无操作即失效；每个用户最多保留 10 个会话，过期会话由后台循环每分钟清理。
- 只有状态为 `active` 的账号能登录；账号不存在与密码错误返回同一提示，密码正确时才告知待审核、已拒绝、已停用。同一 IP 每分钟最多 10 次登录，同一账号连续失败 5 次锁定 15 分钟（进程内计数，单实例部署适用）。
- 带会话 Cookie 的写请求（以及登录请求）必须带允许的 `Origin` 头，否则返回 403；允许来源由 `allowed_origins`（或 `LIUHEN_ALLOWED_ORIGINS`）配置。本地用 http 访问时把 `cookie_secure` 设为 `false`。
- 业务接口依赖 `CurrentUserId`：未登录返回 401，管理员重置密码后、修改密码前返回 403。所有接口的错误格式都是 `{"detail": "提示文字"}`，不带错误码。
- 注册（`POST /auth/register`，同一 IP 每小时 5 次）后账号为 `pending`，管理员通过后才能登录。管理员接口在 `/admin/*`（依赖 `AdminUser`，普通用户 403）：账号列表、审核通过/拒绝、停用/启用（停用立即吊销会话）、重置密码、删除待审核或已拒绝账号、查看账号事件。管理员不能停用或重置自己；停用时同时锁住操作者，保证始终有一个正常的管理员。
- 个人设置：`PATCH /auth/me` 修改邮箱（需当前密码）、`/auth/sessions` 在线设备与下线、`GET /auth/events` 最近 30 天的登录与密码事件。除 `GET /auth/me`、`POST /auth/password` 外，这些接口在强制改密期间同样返回 403。
- 账号事件 `account_events` 只追加，与账号变更同一事务提交：注册、审核、停用/启用、删除、改密/重置、改邮箱、角色变更、登录成功/失败/锁定、下线设备。账号不存在的登录失败不入表；`target_user_id`、`actor_user_id` 不加外键，账号删除后记录仍保留，命令行操作的 `actor_user_id` 为空。
- 每日对话上限：普通用户每个 UTC+08:00 自然日最多新建 `daily_chat_limit` 轮对话（为空时取 `default_daily_chat_limit` / `LIUHEN_DEFAULT_DAILY_CHAT_LIMIT`，默认 50），超出时 `POST /chat` 返回 429；管理员不受限制但照常计数。检查在 `acquire_turn` 新建轮次的同一事务里进行，先锁用户行，并发请求不能突破上限；同一 `request_id` 重试不计数。删除会话时被删轮次的创建时间保留在 `deleted_chat_requests.turn_created_at`，用量统计两处相加，删除会话不能重置用量。`/auth/me` 返回 `chat_usage`；管理员列表与详情返回今日、近 7 天用量，`PATCH /admin/users/{id}` 调整单个用户的上限（`null` 恢复默认）。
- 账号管理命令行：

  ```powershell
  python -m backend.app.cli create-admin --username <名字> [--email <邮箱>]   # 交互输入密码
  python -m backend.app.cli promote --username <名字>
  python -m backend.app.cli reset-password --username <名字>   # 临时密码只显示一次，所有设备下线
  ```

```text
客户端 ── /chat ── chat_service ── agent_runner ── 模型接口
                              │              └─ ToolRegistry
                              │                       │
客户端 ── /tasks、/actions ────┴─────────────── 业务服务
                                                    │
                                                repositories
                                                    │
                                               PostgreSQL

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
python -m pip install -r backend/requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

在项目根目录的 `.env` 填写 `base_url`（完整的聊天接口地址）、`api_key` 和 `model`。Agent 与测评脚本都直接读取该文件；可选的 `judge_base_url`、`judge_api_key`、`judge_model` 用于独立裁判，未填写时沿用 Agent 模型配置。`.env` 已被 Git 忽略，不要提交真实密钥。

在 `.env` 增加 `database_url=postgresql+psycopg://用户名:URL编码后的密码@127.0.0.1:5432/数据库名`。数据库密码包含 `@`、`/` 等字符时须进行 URL 编码；缺少数据库配置时服务会拒绝启动。

表结构由 Alembic 迁移维护（`backend/alembic.ini`、`backend/migrations/`），在项目根目录执行：

```powershell
alembic -c backend/alembic.ini upgrade head   # 新库建表，或把已有库升级到最新
alembic -c backend/alembic.ini current        # 查看当前版本
```

启动时如果数据库版本不是最新，服务会拒绝启动并提示执行 `upgrade head`。引入 Alembic 之前由旧版本自动建表的库，先执行一次 `stamp`：没有 `users` 表用 `stamp 0001`，已有则用 `stamp 0002`，然后再 `upgrade head`。修改 ORM 模型时必须同时新增迁移脚本；`backend/tests/test_migrations.py` 会比对迁移结果与模型，发现漏写的迁移。

启动 API：

```powershell
python -m backend.app.main
```

服务默认监听 `http://127.0.0.1:8000`，交互式接口文档位于 `http://127.0.0.1:8000/docs`。启动时会检查数据库结构版本，并启动提案过期清理任务。测试与测评使用的临时库同样通过迁移建表。PostgreSQL 写入测试可使用 `python -m backend.tests.postgres_smoke`，它创建并清理独立的临时测试库。

## API 概览

| 方法与路径 | 用途 |
| --- | --- |
| `POST /auth/login` | 用户名或邮箱 + 密码登录，`remember` 控制是否保持登录 7 天 |
| `POST /auth/logout` | 删除当前会话并清除 Cookie |
| `GET /auth/me` | 当前用户信息（含是否需要修改密码） |
| `POST /auth/password` | 修改密码；其他设备的会话全部失效 |
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

测评通过 `backend/app/evals/` 运行真实模型。每次运行使用独立的临时数据库，库里有测评用户和"另一个用户"两个账号；测评用户状态正常、每日上限设为 1000，不受部署环境默认上限影响。业务准确性由程序给出 0/100 分；回复质量由 LLM 裁判给出 0/33/67/100 分。裁判失败时回复分为 `null`，报告会保留错误原因。

- **测评时间**：默认使用固定时间点 `standard`（2026-11-18 周三 10:00），"明天""下周三"在任何日子运行含义都一样；`--anchors all` 另外在周日晚上（跨周）和 12 月 31 日（跨年）各跑一遍；`--now` 可指定任意时间。
- **跨用户隔离**：每个场景都检查另一个用户的任务、提案和审计没有变化，且它的任务标题（带"B机密"标记）没有出现在工具结果或回复里；第 22–24 题专门在另一个用户名下预置同名任务、用他人任务 ID 和"我是管理员"话术试探越权。
- **模板回复不送裁判**：最后一轮成功生成提案时，回复由 `format_proposal` 按提案快照生成，裁判结果记为跳过（`skipped: template_reply`），提案内容由业务分检查。
- **稳定性**：汇总里除平均分外，给出同一场景、变体、时间点多次运行都通过的比例（`business_all_runs_pass_rate`），并列出没有每次都通过的组（`unstable_or_failing`）。

业务分按每题预期判定：普通创建题检查任务是否正确落库；故障注入题检查预期错误是否被正确处理、是否避免误创建或半修改。因此第 12、15 题在注入故障后安全失败，业务分仍可为 100；用户目标是否最终完成可以另设指标。回复分允许语义等价和上下文省略：多轮对话中用户已知的旧冲突、并发落败请求的 HTTP 码、以及无效输入后隐含的“尚未创建”，不因未重复表述而扣分；事实错误和误报成功仍为 0。

### 测评流水线

一次测评分两半：**运行**（模型 + 提示词 + 场景 → 证据：对话、工具调用、最终任务/提案/审计状态）和**评分**（证据 + 评分规则 → 分数）。报告 JSON 保存完整证据，业务评分只读这些证据，不调用模型也不访问数据库。因此命令按"要回答的问题"分三层，改了什么就用哪一层：

| 层 | 回答的问题 | 什么时候用 | 是否运行 Agent |
| --- | --- | --- | --- |
| 1. 运行测评 | Agent 做得好不好 | 日常；改了提示词、工具或 Agent 代码 | 是 |
| 2. 只重新评分 | 评分规则对不对 | 只改了 `grading.py` 或裁判要点 | 否 |
| 3. 校准裁判 | 裁判模型可不可信 | 换裁判模型、改裁判提示词后，或测评集大改后 | 否 |

同时改了提示词和评分规则时，先用第 2 层对旧报告重新评分，单独确认规则改动的效果，再用第 1 层重跑；否则分数变化分不清来自规则还是模型的随机性。

#### 1. 运行测评

在项目根目录执行。日常只需要前两条：

```powershell
python -m backend.app.evals.run --case 03 --trials 3   # 改完提示词后先验证相关场景
python -m backend.app.evals.run --all --trials 3       # 确认没有影响其他场景
python -m backend.app.evals.run --all --trials 3 --anchors all   # 改了时间相关逻辑时：三个时间点，记录数乘 3
```

全量包含 26 条场景；第 14 条每次覆盖三种确认／取消并发变体，因此每轮 28 份记录，运行 3 轮共 84 份（`--anchors all` 时 252 份）。报告默认保存为 `backend/evals/results/<场景号或 all>.json`，同名文件已存在时自动另存为 `-2`、`-3` 等，不会覆盖；也可用 `--output` 指定路径。终端最后打印汇总，重点看 `unstable_or_failing`。查看某份报告的汇总：

```powershell
$report = Get-Content backend/evals/results/all.json -Raw | ConvertFrom-Json
$report.summary
```

#### 2. 只重新评分

用当前的评分规则和裁判，对已有报告里的证据重新打分，不再次运行 Agent。业务分不花模型费用，回复分只调用裁判。结果另存为新文件，原报告不变：

```powershell
python -m backend.app.evals.run --grade-report backend/evals/results/all.json
```

#### 3. 校准裁判

从已评分报告抽取约 20% 供人工盲审。相同报告与 `seed` 会选出相同记录；当前策略优先覆盖不同分档和复杂场景，再随机补齐：

```powershell
python -m backend.app.evals.calibration sample backend/evals/results/all.json --rate 0.2 --seed 42
```

在生成的 `backend/evals/reviews/all-review.json` 中，为每条记录填写 `human_business_score`（0/100）、`human_reply_score`（0/33/67/100）和 `human_reason`；可选填 `human_factual_error`、`human_forbidden_hit`。盲审文件不包含自动分数。完成后比较人工与自动评分：

```powershell
python -m backend.app.evals.calibration compare backend/evals/reviews/all-review.json
```

对比报告给出一致率、平均分差与严重漏判清单。`compare` 会核对抽样时原报告的 SHA256：原报告被修改或删除后无法对比，移动了位置可用 `--report` 指定新路径。因此做过人工复核的报告要保留到对比完成。

`backend/evals/results/` 和 `backend/evals/reviews/` 已被 Git 忽略。

## 自动化测试与当前范围

后端回归测试位于 `backend/tests/`，可从项目根目录运行：

```powershell
python -m unittest discover -s backend/tests -v
```

生产对话自动转为测评候选案例的流程尚未实现；目前的 26 条测评场景由人工维护。
