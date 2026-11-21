# CLAUDE.md

本文件为 Claude 在留痕（liuhen）仓库中工作时的指引，内容整理自 `AGENTS.md`、根目录及各子目录 `README.md`。规则冲突时以 `AGENTS.md` 和作者在对话中的明确要求为准。

## 项目简介

留痕是面向个人的任务管理 AI Agent。用户用自然语言创建和查询任务；Agent 对任务的修改、删除只生成提案，由用户确认后执行。后端持久化对话、工具调用结果，并把业务变更写入只追加的审计日志。

- 优先级：可靠性、可解释性、操作留痕、可验证性，而不是堆功能。
- 当前已实现：任务管理、AI 对话与提案确认、操作留痕、Vue 3 前端（对话、待办、日历、留痕页）、后端测试与 Agent 测评。
- 多用户改造按 `docs/prd-multi-user.md` 分阶段进行。已完成：业务数据按 `user_id` 隔离；账号密码登录（Argon2id 哈希、服务端会话 + HttpOnly Cookie、强制改密、Origin 校验、登录限流）；账号管理命令行 `python -m backend.app.cli`。尚未实现：注册与审核、管理员页面与接口、账号事件、个人设置、每日对话上限（P4、P5）。
- 尚未实现（不要因路线图提前实现）：提醒主动推送、文档管理后端与 Agent 工具、偏好管理、SSE 流式接口、会话列表/历史的 HTTP 接口。前端资料库只是原型示例。

## 技术栈与结构

- 后端：Python 3.11+、FastAPI、Pydantic、SQLAlchemy、PostgreSQL；`.env` 由 `python-dotenv` 读取，模型请求走 HTTP。
- 前端：Vue 3、TypeScript、Vite、Vue Router；共享状态用 `src/state.ts` 的响应式对象（未引入 Pinia）；视觉以 `frontend/reference/index.html` 原型为准。
- 部署：Docker Compose + Caddy（HTTPS、Basic Auth 保护 `/app/` 和 API），FastAPI 与 PostgreSQL 只在内部网络开放。

| 路径 | 内容 |
| --- | --- |
| `backend/app/api/` | FastAPI 路由（chat、tasks、actions、audit） |
| `backend/app/agent/` | 模型调用、Agent 循环、工具注册（`tool_registry.py`） |
| `backend/app/services/` | 任务、提案、对话、幂等请求与审计的业务逻辑 |
| `backend/app/repositories/` | 任务和提案的数据访问 |
| `backend/app/models/`、`schemas/` | SQLAlchemy 表模型、Pydantic 请求/响应模型 |
| `backend/app/db/`、`config/` | 数据库连接、迁移调用、时间与模型配置 |
| `backend/migrations/` | Alembic 迁移脚本（`alembic.ini` 在 `backend/`） |
| `backend/app/evals/` | 真实模型测评、评分、裁判与人工校准 |
| `backend/tests/` | 后端自动化测试 |
| `frontend/app/` | Vue 3 前端应用 |
| `frontend/reference/` | 前端视觉原型 |
| `deploy/` | Compose、Caddyfile、Dockerfile 与部署脚本 |
| `线上问题记录.md` | 真实线上问题记录 |

请求链路：`/chat` → `chat_service` → `agent_runner` → 模型接口 / `ToolRegistry` → 业务服务 → repositories → PostgreSQL；`/tasks`、`/actions` 直接进入业务服务；`/audit/*` → `audit_service` → `operation_events`。

## 常用命令（PowerShell，在项目根目录执行）

```powershell
# 环境准备
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend/requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }

# 数据库迁移（新库建表或升级到最新；旧库首次需先 stamp，见 backend/README.md）
alembic -c backend/alembic.ini upgrade head

# 账号管理（交互输入密码；临时密码只显示一次）
python -m backend.app.cli create-admin --username <名字>
python -m backend.app.cli reset-password --username <名字>

# 启动后端（http://127.0.0.1:8000，文档 /docs）
python -m backend.app.main

# 启动前端（http://127.0.0.1:5173/，另开终端）
cd frontend/app
npm ci          # 仅首次或依赖变化时；Windows 上先停掉正在运行的 dev server，避免 EPERM
npm run dev
npm run build   # TypeScript 检查 + 生产构建

# 后端测试
python -m unittest discover -s backend/tests -v
python -m backend.tests.postgres_smoke   # 使用独立临时库的 PostgreSQL 写入冒烟测试

# 测评（真实模型、隔离临时数据库）
python -m backend.app.evals.run --case 01 --trials 3 --now 2026-11-18T10:00:00+08:00
python -m backend.app.evals.run --all --trials 3 --now 2026-11-18T10:00:00+08:00
python -m backend.app.evals.run --grade-report backend/evals/results/all.json   # 只重新评分
python -m backend.app.evals.calibration sample backend/evals/results/all.json --rate 0.2 --seed 42
python -m backend.app.evals.calibration compare backend/evals/reviews/all-review.json
```

部署相关（服务器上）：

```bash
docker compose -f deploy/compose.yaml config --quiet
docker compose -f deploy/compose.yaml build api web
docker compose -f deploy/compose.yaml run --rm api alembic -c backend/alembic.ini upgrade head
docker compose -f deploy/compose.yaml up -d
docker compose -f deploy/compose.yaml ps
docker compose -f deploy/compose.yaml logs --tail=100
python3 deploy/verify_site.py
```

`.env` 需填写 `base_url`（完整聊天接口地址）、`api_key`、`model`、`database_url`（`postgresql+psycopg://...`，密码需 URL 编码）；可选 `judge_base_url`、`judge_api_key`、`judge_model`；本地用 http 访问前端时设 `cookie_secure=false`，`allowed_origins` 默认是本地 Vite 地址。缺数据库配置时服务拒绝启动。表结构由 Alembic 迁移维护（`backend/migrations/`），数据库版本不是最新时服务拒绝启动；修改 ORM 模型必须同时新增迁移脚本，`test_migrations.py` 会比对迁移结果与模型（`EXCLUDE` 约束只按名字核对）。

## 核心业务规则（改代码前需理解）

- 任务至少有 `start_at` 或 `due_at`。截止时间不由开始时间和时长推算；只有同时给出 `start_at` 和 `duration_minutes` 才计算 `end_at` 并做冲突检查。未带时区的时间按 UTC+08:00 解释。
- Agent 可直接创建任务；修改、删除只生成 `pending_actions` 提案，经 `POST /actions/{id}/confirm` 或 `/cancel` 决定。提案有效期 10 分钟，后台每 60 秒清理到期提案。
- 提案状态：`pending`、`executed`、`cancelled`、`expired`、`replaced`、`conflicted`、`failed`。确认与取消通过事务和条件状态转换竞争，确认还会核对任务版本。
- 页面手动修改/删除走 `PATCH`/`DELETE /tasks/{id}`，必须带 `If-Match` 当前版本，版本过旧返回 409；手动变更会把该任务待确认的提案标记为 `replaced`。
- 幂等：`POST /chat` 每个新意图一个 UUID `request_id`，重试复用；`POST /tasks` 用 UUID `Idempotency-Key`。同一会话同一时间只处理一轮。
- 审计事件与业务变更在同一事务中提交；删除会话不删除任务、提案和审计。

## 编码约定

- Agent 工具只在 `agent/tool_registry.py` 显式注册。不要因为 API 已有某能力就把它注册为工具；新增工具由作者按开发阶段明确推进。
- 绝不把"确认执行提案"注册为 Agent 可自主调用的工具；模型生成的参数仍须校验。
- 数据隔离：当前用户只从依赖 `CurrentUserId` 取得（Agent 工具从本轮 `chat_turns.user_id` 取得），绝不接受请求体、查询参数或工具参数中的 `user_id`。服务层与仓储层的 `user_id` 一律是仅限关键字参数；按客户端提供的 ID 查找和列表查询必须带 `user_id` 条件，查不到与无权访问一样返回 404。`request_id`、`Idempotency-Key` 命中别人的记录时返回 409，不回放内容。新增业务路由必须依赖 `CurrentUserId`（`test_user_isolation.py` 会检查）。
- 分层：路由 → 服务层（校验、事务、审计）→ 仓储层（数据库读写）。保持现有实现，避免为设计模式增加不必要的类或抽象。
- 不要求用户填写 ISO 时间、时区或工具参数；只有开始时间、任务目标或修改意图无法确定时才追问，不擅自猜测。
- 工具失败时不得声称成功，对用户说明实际执行结果。
- API 密钥只从环境配置读取，不硬编码，不要求用户提供真实密钥；`.env`、`deploy/secrets/`、测评结果（`backend/evals/results/`、`reviews/`）与数据库备份不得提交。
- 前端：助手回复按 Markdown 渲染并禁用原始 HTML、清理输出；后端没有的字段（分类、备注、子任务等）保留原型布局，但不能把未保存的数据显示为成功。
- 每完成一个开发阶段，清理过渡实现、一次性脚本、旧兼容分支、重复逻辑以及失效配置和文档。
- 允许为迁移、数据修复或阶段之间的中间状态编写临时代码，但必须在代码处标注 `TEMP(移除条件): 用途`（Python 用 `# TEMP(...)`，TS/Vue 用 `// TEMP(...)`），写明为什么存在、什么时候删；该阶段结束时一并删除，并用 `grep -rn "TEMP("` 确认没有遗留。不带标记的过渡代码视为冗余。

## 测试与验证

- 验证创建流程优先使用隔离数据库；不要为测试反复执行会写入真实数据库的创建提示词。
- 验证 Agent 时同时关注最终业务状态与工具调用过程。
- 测评共 21 条场景（第 14 条每次覆盖三种并发变体，3 轮共 69 份记录）。业务分 0/100 由程序判定；回复分 0/33/67/100 由 LLM 裁判给出，裁判失败为 `null`。`backend/evals/results/`、`reviews/` 已被 Git 忽略。
- `线上问题记录.md` 只记录实际发生的问题，追加不覆盖；未核实内容标注"待确认"，不推测根因或宣称已修复。

## 与作者协作

- 始终使用中文与作者交流（包括回复、讲解、提问和总结）；代码、命令、标识符保持原样。
- 作者有 Java 后端经验，正在通过亲手开发学习 Python 后端和 Agent 底层实现。讲解新概念时优先用 Java 类比，并指出运行机制差异。
- 先讲原理和设计理由，再推进代码；一次聚焦一个核心模块，给出作者能亲手完成的小步骤。默认以指导和审查为主；作者明确要求修改时，直接实施并做必要验证。
- 审查作者代码时，先指出具体问题及原因，再建议最小修改。
- 涉及函数对象、绑定方法、协程、异步上下文管理器或依赖注入时，解释实际执行过程，而不只给语法。

## Git 工作流约定

- 不直接在 `main` 上开发：每个开发阶段在特性分支（如 `feat/reminder`）上进行；`main` 只接收整理好的阶段性提交。
- Claude 每完成一次改动就自动提交到当前特性分支，作为回退点；本地历史不求整洁，信息写清改了什么即可（允许 `wip:` 前缀）。若当前在 `main` 上，先新建特性分支再提交，不直接提交到 `main`。
- 修正已提交的内容时追加修正提交（如 `git commit --fixup <提交>`），不回头改写分支历史。
- 作者说"阶段完成"时：检查 `grep -rn "TEMP("` 无遗留、跑后端测试（前端有改动时再跑 `npm run build`），然后 `git merge --squash` 合入 `main`，提交信息概括整个阶段。
- `push` 是发布边界：只在作者明确要求时推送，只推整理好的 `main`；已推送的提交不再改写，除非作者明确要求并知晓需要强推。
