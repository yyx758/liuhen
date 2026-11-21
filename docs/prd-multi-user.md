# PRD：多用户支持（账号、认证、数据隔离、注册审核）

- 状态：草案 v0.2（2026-11-21，第 15 节待讨论问题已全部确认）
- 适用范围：后端 `backend/`、前端 `frontend/app/`、部署 `deploy/`
- 关联：`CLAUDE.md`、`AGENTS.md`、设计稿《个人助手 · 界面设计》中的「登录」画板

本文是多用户阶段的开发指引。第 3 节列出已和作者确认的决策；第 15 节记录讨论过程中的问题及结论，新发现的不确定点也追加到第 15 节，确认后再实现。

---

## 1. 背景

留痕目前是单用户系统：

- 业务表（`tasks`、`pending_actions`、`conversations`、`conversation_messages`、`chat_turns`、`operation_events`、`task_create_requests`、`deleted_chat_requests`）都没有归属用户的字段。
- `tasks` 的时间冲突排他约束 `ex_tasks_no_overlap` 在全表范围内生效：两个不同用户的任务时间重叠也会冲突。
- Agent 工具（`agent/tool_registry.py`）只拿到 `Session`，不知道"当前是谁"。
- 整站只靠 Caddy Basic Auth 保护，所有人共用一套凭据。
- 已有 `users` 表（`backend/app/models/user.py`），但没有注册、登录或会话接口；前端 `LoginPage.vue` 只有静态表单。
- 前端对话列表存在 `localStorage`（`state.ts` 中的 `CHAT_KEY`），不区分用户。
- 启动时只执行 `create_all`，不升级已有表结构。

## 2. 目标与非目标

### 2.1 目标

1. **数据隔离**：每个用户只能读写自己的任务、提案、对话和留痕，在 HTTP 接口、Agent 工具、数据库约束三层都成立。
2. **用户认证**：用户名或邮箱加密码登录，服务端会话，可随时吊销。
3. **注册审核**：开放注册，管理员审核通过后才能使用。
4. **账号管理**：管理员可以审核、禁用/启用账号、重置密码、调整用量上限；看不到他人的业务数据。
5. **成本控制**：限制每个用户每日的 AI 对话轮数。
6. **可留痕**：账号相关的关键操作写入只追加的账号事件表。
7. **前端**：登录页接入后端，新增注册页、修改密码页、管理后台页，加路由守卫。

### 2.2 非目标（本阶段不做）

- 邮箱验证、邮件找回密码、短信、第三方登录（微信/GitHub 等）、二次验证（2FA）。
- 团队、共享任务、多角色细粒度权限（RBAC 只分 `user` / `admin` 两级）。
- 会话列表/历史的 HTTP 接口（前端对话列表仍存 `localStorage`，但按用户隔离，见 10.6）。
- 提醒推送、文档管理、偏好管理（路线图另行推进）。
- 用户自助注销账号、数据导出：需要注销时由管理员禁用账号。注销和"审计只追加"有冲突，以后单独设计。
- 修改用户名。
- 审核提醒走邮件、推送或 Webhook：第一期只在界面上显示角标。

## 3. 已确认决策

| 编号 | 议题 | 决策 |
| --- | --- | --- |
| D1 | 账号来源 | 开放注册，状态为"待审核"，管理员通过后才能登录 |
| D2 | 管理员数据权限 | 只管理账号，不能查看或修改他人任务、对话、留痕；能看账号信息和用量统计 |
| D3 | 认证方案 | 服务端会话，存 PostgreSQL；浏览器只持有 HttpOnly Cookie 中的随机令牌 |
| D4 | 账号标识 / 找回密码 | 用户名必填、邮箱可选；登录可用用户名或邮箱；忘记密码由管理员重置为临时密码，首次登录强制修改 |
| D5 | 存量数据 | ~~存量数据归属初始管理员~~，已被 D17 取代；初始管理员仍由 CLI 创建 |
| D6 | 表结构迁移 | 引入 Alembic，启动不再依赖 `create_all` 建表 |
| D7 | Basic Auth | 上线应用登录后移除 Caddy Basic Auth，改为应用层登录加限流 |
| D8 | 用量限制 | 按用户统计每日 `/chat` 轮数，默认上限可配置，管理员可单独调整 |
| D9 | 默认每日上限 | 50 轮 |
| D10 | 用户查看登录记录 | 可以：个人设置中显示在线会话和最近的登录事件，可以下线单个设备或一键下线其他设备 |
| D11 | 自助注销账号 | 第一期不做 |
| D12 | 管理员用量统计 | 账号列表显示今日轮数/上限和近 7 天合计，直接从 `chat_turns` 统计，不加新表 |
| D13 | 品牌名 | 界面上沿用"小记"；"留痕"只用作项目、仓库和代码名称 |
| D14 | 资料修改 | 用户名不可修改；邮箱可以在个人设置中修改或清空，需要输入当前密码 |
| D15 | 审核提醒 | 只在界面显示角标（管理员的导航入口和后台"待审核"Tab），不接入邮件、推送或 Webhook |
| D17 | 存量数据 | 不迁移存量数据，上线时备份后重建数据库（取代 D5 中"存量数据归属初始管理员"）；迁移 `0003` 在业务表已有数据时拒绝执行 |
| D16 | 管理员对话用量 | `role = admin` 的账号不受每日对话轮数限制；管理员以管理员身份登录前端时，导航中显示"管理"入口 |
| D18 | 错误响应格式 | 所有接口出错时只返回给用户看的文字 `{"detail": "文字"}`，不带错误码；前端按 HTTP 状态码判断（如 401 跳登录页） |
| D19 | 测评补充 | 跨用户测评场景与真实模型回归放到多用户需求全部完成、调整测评集之后再做 |

## 4. 角色与账号状态

### 4.1 角色

| 角色 | 说明 |
| --- | --- |
| `user` | 普通用户，管理自己的任务、对话、留痕 |
| `admin` | 本身也是普通用户（有自己的任务数据），另外可以访问 `/admin/*` 账号管理功能；AI 对话不限量（D16） |

管理员只能通过 CLI 产生（初始化或提升），第一期不提供界面上的角色变更。

### 4.2 账号状态机

```
         注册
          │
          ▼
      ┌────────┐  管理员拒绝   ┌──────────┐
      │pending │ ───────────▶ │ rejected │
      └────────┘              └──────────┘
          │ 管理员通过
          ▼
      ┌────────┐  管理员禁用   ┌──────────┐
      │ active │ ◀──────────▶ │ disabled │
      └────────┘  管理员启用   └──────────┘
```

- 只有 `active` 可以登录和调用业务接口。
- 进入 `disabled` 时立即吊销该用户所有会话；正在处理的 `/chat` 轮次允许跑完，但之后的请求返回 401。
- `rejected` 账号保留记录（用户名仍被占用），管理员可删除 `pending` / `rejected` 账号以释放用户名（这两种状态不会有业务数据）。
- 管理员不能禁用自己，系统必须保留至少一个 `active` 的 `admin`。

## 5. 功能需求

### 5.1 注册

- 字段：用户名（必填）、密码、确认密码（只在前端校验）、邮箱（可选）。
- 用户名规则：3–32 位，限 `a-z`、`0-9`、`_`；保存前转小写，按小写唯一。
- 邮箱：可选；保存前去空格并转小写，唯一。
- 密码规则：8–128 位，不强制字符组合；拒绝与用户名相同的密码。
- 注册成功后账号为 `pending`，**不自动登录**；页面提示"已提交，等待管理员审核"。
- 用户名或邮箱已存在时提示"用户名已被占用" / "邮箱已被使用"。（注册接口本来就会暴露用户名是否存在，这是可接受的；登录接口不暴露，见 5.2）
- 注册接口按 IP 限流（见 7.5）。

### 5.2 登录

- 输入"邮箱或用户名"加密码；含 `@` 时按邮箱查，否则按用户名查（都转小写）。
- 校验顺序和提示：
  1. 账号不存在或密码错误 → 统一提示"账号或密码错误"（账号不存在时也要执行一次哈希校验，避免通过响应时间区分）。
  2. 密码正确，状态为 `pending` → "账号正在审核中"；`rejected` → "注册申请未通过"；`disabled` → "账号已被停用，请联系管理员"。只有密码正确才返回这些状态，不向不知道密码的人泄露状态。
  3. 密码正确且 `active` → 创建会话、设置 Cookie，更新 `last_login_at`。
- "保持登录 7 天"：
  - 勾选：Cookie 设 `Max-Age=7天`，服务端会话绝对有效期 7 天。
  - 不勾选：会话 Cookie（关闭浏览器即失效），服务端会话 12 小时无操作过期。
- 登录成功后若 `must_change_password = true`，前端只允许进入修改密码页。
- 登录接口限流（见 7.5）。

### 5.3 登出

- `POST /auth/logout` 删除当前会话并清除 Cookie。
- 前端同时清空当前用户在内存中的状态（见 10.6）。

### 5.4 修改密码

- 需要输入旧密码和新密码；新密码规则同注册。
- 成功后吊销该用户**其他**会话，当前会话保留；清除 `must_change_password`。
- 强制改密状态下（管理员重置后首次登录），除 `GET /auth/me`、`POST /auth/password`、`POST /auth/logout` 外，其余接口返回 403。

### 5.5 忘记密码

- 登录页"忘记密码"点击后显示说明："请联系管理员重置密码"。不做自助流程。
- 管理员重置：系统生成随机临时密码（16 位以上），**只在本次响应中显示一次**；同时设置 `must_change_password = true` 并吊销该用户所有会话。临时密码由管理员线下告知用户。

### 5.6 管理员：账号管理

管理后台（`/admin`）功能：

| 功能 | 说明 |
| --- | --- |
| 账号列表 | 按状态筛选（默认显示待审核），按用户名搜索，分页；字段：用户名、邮箱、角色、状态、注册时间、最后登录时间、今日对话轮数/上限、近 7 天对话轮数合计 |
| 待审核角标 | 管理员的导航入口和后台"待审核"Tab 上显示待审核账号数量（打开页面或刷新时获取，不做实时推送） |
| 审核 | `pending` → 通过（`active`）或拒绝（`rejected`，可填写原因） |
| 禁用/启用 | `active` ⇄ `disabled`，可填写原因 |
| 重置密码 | 见 5.5 |
| 调整用量上限 | 设置该用户每日对话轮数上限；留空表示使用全局默认值 |
| 删除账号 | 只能删除 `pending` / `rejected` 账号 |
| 账号事件 | 查看账号事件记录（见第 8 节） |

管理员**不能**：查看他人任务、提案、对话、操作留痕；以他人身份登录；修改他人业务数据。

### 5.7 初始管理员与 CLI

提供命令行（不暴露 HTTP 接口）：

```powershell
python -m backend.app.cli create-admin --username <名字>   # 交互输入密码，创建 active 的 admin
python -m backend.app.cli promote --username <名字>        # 把已有 active 用户提升为 admin
python -m backend.app.cli reset-password --username <名字>  # 管理员也忘记密码时的兜底
```

CLI 操作同样写账号事件，`actor_user_id` 为空、`details` 中标注来源为 `cli`。

### 5.8 个人设置

所有已登录用户都可以使用：

| 功能 | 说明 |
| --- | --- |
| 基本信息 | 显示用户名（只读）、邮箱、角色、注册时间、今日对话轮数/上限 |
| 修改邮箱 | 可以修改或清空，需要输入当前密码；规则同注册（唯一、转小写）；写 `email_changed` 账号事件 |
| 修改密码 | 见 5.4 |
| 在线设备 | 列出当前用户未过期的会话：登录时间、最近活动时间、IP、设备（由 User-Agent 简单解析为"浏览器 / 系统"），标出"当前设备" |
| 下线设备 | 可以下线单个其他设备，或一键"退出其他所有设备"；当前会话不在可下线列表里（下线当前设备就是登出） |
| 登录记录 | 最近 30 天的 `login_succeeded`、`login_failed`、`login_locked`、`password_changed`、`password_reset` 事件，显示时间和 IP |

用户名不可修改（D14）。

## 6. 数据隔离设计

### 6.1 原则

1. **归属从会话来，不从参数来**：`user_id` 只由认证依赖（`api/deps.py` 的 `CurrentUserId`）从会话里得到，绝不接受请求体、查询参数或模型生成的工具参数中的 `user_id`。
2. **服务层必须带 `user_id`**：读写业务数据的服务函数和入口仓储函数都有仅限关键字的 `user_id` 参数（`*, user_id`，避免和同为 `int` 的 `task_id`、`action_id` 传错位置）。按客户端提供的 ID 查找、列表查询一律带 `WHERE user_id = :user_id`；已在入口校验过归属的行，后续按主键做的状态转换、版本条件更新不再重复过滤。后台定时任务（6.5）全局扫描，写审计时使用行自身的 `user_id`。
3. **不存在和无权访问一律 404**：访问别人的任务、提案、会话时返回与"不存在"相同的 404，不返回 403，避免通过自增 ID 枚举出他人数据。
4. **数据库兜底**：外键和约束层面也体现归属，防止应用层遗漏时写出跨用户关联。

### 6.2 表结构变更

| 表 | 变更 |
| --- | --- |
| `users` | 新增 `role`（`user`/`admin`）、`status`（`pending`/`active`/`rejected`/`disabled`，替换 `is_active`）、`must_change_password`、`daily_chat_limit`（可空，空表示用默认值）、`last_login_at`、`reviewed_at`、`reviewed_by`；`username`、`email` 按小写唯一 |
| `user_sessions`（新） | `id`、`user_id`、`token_hash`（唯一）、`created_at`、`last_seen_at`、`expires_at`、`idle_timeout_seconds`（可空）、`user_agent`、`ip`；见 7.2 |
| `account_events`（新） | 账号事件只追加表，见第 8 节 |
| `tasks` | 新增 `user_id NOT NULL` 外键 → `users.id`；索引 `(user_id, start_at)`、`(user_id, due_at)` |
| `pending_actions` | 新增 `user_id NOT NULL`；索引 `(user_id, status)` |
| `conversations` | 新增 `user_id NOT NULL` |
| `conversation_messages` | 不加字段，通过 `conversation_id` 继承归属 |
| `chat_turns` | 新增 `user_id NOT NULL`、`created_at`（用于用量统计）；索引 `(user_id, created_at)` |
| `task_create_requests` | 新增 `user_id NOT NULL` |
| `deleted_chat_requests` | 新增 `user_id NOT NULL` |
| `operation_events` | 新增 `user_id NOT NULL`；索引 `(user_id, id)`、`(user_id, task_id, id)` |

时间冲突排他约束改为按用户：

```sql
-- 需要 btree_gist 扩展，才能在 GiST 索引里对整数用 "="
CREATE EXTENSION IF NOT EXISTS btree_gist;

ALTER TABLE tasks DROP CONSTRAINT ex_tasks_no_overlap;
ALTER TABLE tasks ADD CONSTRAINT ex_tasks_no_overlap
  EXCLUDE USING gist (user_id WITH =, tsrange(start_at, end_at, '[)') WITH &&)
  WHERE (status = 'todo' AND start_at IS NOT NULL AND end_at IS NOT NULL);
```

`repositories/task_repository.find_overlapping_task` 的应用层冲突查询也要带 `user_id`。

### 6.3 客户端提供的 ID：防串号

以下 ID 由客户端生成或传入，全局唯一，必须校验归属：

| ID | 风险 | 规则 |
| --- | --- | --- |
| `POST /chat` 的 `conversation_id` | 往别人的会话追加消息、读取别人的历史 | 会话存在但不属于当前用户 → 404 |
| `POST /chat` 的 `request_id` | 用户 B 用 A 的 `request_id` 重放，拿到 A 的回答 | `chat_turns` / `deleted_chat_requests` 中已存在且属于别人 → 409，不返回任何内容 |
| `POST /tasks` 的 `Idempotency-Key` | 拿到 A 创建任务的结果快照 | 同上，`task_create_requests` 中属于别人 → 409 |
| 路径中的 `task_id` / `action_id` | 按自增 ID 枚举 | 查询带 `user_id`，查不到 → 404 |

主键保持全局 UUID 不变（不改成联合主键），以免改动幂等逻辑；归属校验放在服务层。

### 6.4 Agent 与工具

- `chat_service.send_message` 增加 `user_id` 参数，经 `agent_runner` → `chat_turn_service.execute_tool` → `ToolRegistry.execute_tool` 一路传到每个工具函数。
- 工具函数签名为 `(data, session, *, user_id)`；`chat_turn_service.execute_tool` 使用本轮已保存的 `chat_turns.user_id`，而不是调用方另传的值。
- 工具参数 schema 中**不出现** `user_id`；即使模型在参数里塞了 `user_id` 也会被 Pydantic 拒绝或忽略（工具参数模型保持 `extra="forbid"`）。
- `list_tasks`、`get_task`、`get_action`、`propose_update`、`propose_delete`、创建任务等工具全部只在当前用户范围内操作；查不到时返回"未找到"，不提示"属于别人"。
- 系统提示中不放入其他用户的任何信息；可以放当前用户的用户名，供回复称呼（可选）。
- "确认执行提案"仍然只通过 `POST /actions/{id}/confirm`，且该接口校验提案属于当前用户。

### 6.5 后台任务

- `proposal_expiry_loop` 继续全局扫描到期提案（它不对外返回数据），但写入审计事件时带上提案的 `user_id`。
- 定期清理过期会话（`user_sessions.expires_at` 已过），可以挂在同一个循环里。

### 6.6 审计（操作留痕）

- `audit_service.record_event` 增加必填 `user_id`，与业务变更在同一事务提交（现有规则不变）。
- `/audit/events`、`/audit/events/page`、`/audit/summary` 只返回当前用户的事件。
- 删除会话仍不删除任务、提案和审计（现有规则不变）。

## 7. 认证与会话

### 7.1 密码存储

- 使用 Argon2id（`argon2-cffi`），保存完整编码串（含参数和盐）到 `password_hash`。
- 登录成功时若哈希参数已过时（`check_needs_rehash`），用新参数重新哈希保存。
- 任何日志、审计、异常信息中都不得出现明文密码或临时密码（重置接口的响应除外）。

### 7.2 会话

- 登录成功后生成 32 字节随机令牌（`secrets.token_urlsafe(32)`），**数据库只存 SHA-256 哈希**，令牌原文只放在 Cookie 里。数据库泄露时无法直接冒充用户。
- Cookie：名称 `liuhen_session`，属性 `HttpOnly; Secure; SameSite=Lax; Path=/`；本地开发（http）时去掉 `Secure`，由配置控制。
- 每次请求：按令牌哈希查会话 → 检查未过期 → 检查用户 `status = active` → 更新 `last_seen_at`（同一会话 1 分钟内最多写一次，避免每个请求都写库）。
- 吊销时机：登出、修改密码（吊销其他会话）、管理员禁用、管理员重置密码。
- 单用户会话数上限 10，超出时删除最早的会话。

### 7.3 FastAPI 依赖

```python
CurrentUser = Annotated[UserORM, Depends(require_user)]    # 需要 active 用户且不处于强制改密
AdminUser   = Annotated[UserORM, Depends(require_admin)]   # 在 require_user 基础上要求 role = admin
```

- 所有业务路由（`tasks`、`actions`、`chat`、`audit`）都加 `CurrentUser`；`/admin/*` 加 `AdminUser`。
- 不使用"全局中间件 + 白名单"，而是在路由上显式声明依赖，便于审查哪些接口受保护。增加一个测试遍历 `app.routes`，确认除白名单（`/auth/login`、`/auth/register`、`/docs`、`/openapi.json` 等）外的路由都依赖 `require_user`。

### 7.4 CSRF

- `SameSite=Lax` 已阻止跨站 POST/PATCH/DELETE 携带 Cookie。
- 再加一层：**带会话 Cookie 的**非 GET 请求，以及 `POST /auth/login`，校验 `Origin` 头（缺失或不在允许列表时拒绝 403）。不带 Cookie 的写请求本来就会 401，不需要校验。允许列表来自配置（生产为站点域名，开发为 `http://127.0.0.1:5173` 等）。

### 7.5 限流

在进程内实现（单实例部署足够；多实例时再迁移）：

| 接口 | 规则 |
| --- | --- |
| `POST /auth/login` | 同一 IP 每分钟 10 次；同一账号连续失败 5 次后锁定 15 分钟（锁定期间统一返回"账号或密码错误"之外的"尝试次数过多，请稍后再试"） |
| `POST /auth/register` | 同一 IP 每小时 5 次 |

IP 取 Caddy 传入的 `X-Forwarded-For`，只信任来自内部网络的代理。

## 8. 账号事件（账号留痕）

新表 `account_events`，只追加：

| 字段 | 说明 |
| --- | --- |
| `id` | 主键 |
| `event_type` | `user_registered`、`user_approved`、`user_rejected`、`user_disabled`、`user_enabled`、`user_deleted`、`password_changed`、`password_reset`、`email_changed`、`role_changed`、`chat_limit_changed`、`login_succeeded`、`login_failed`、`login_locked`、`session_revoked` |
| `target_user_id` | 被操作的账号（`user_deleted` 后外键不能约束，存普通整数） |
| `actor_user_id` | 操作者；本人操作时等于目标，CLI 操作为空 |
| `reason` | 管理员填写的原因 |
| `details` | JSON，如变更前后的值、来源（`web`/`cli`）、IP |
| `created_at` | UTC 时间戳 |

- 与账号变更在同一事务提交。
- `login_failed` 只记录能对应到已存在账号的失败（账号不存在的失败只计入限流，不入表）。
- 管理员可在后台查看全部账号事件；用户可通过 `GET /auth/events` 查看自己的登录与密码相关事件（D10，见 5.8）。
- 账号事件**与业务留痕分开**：管理员能看账号事件，但看不到业务留痕（符合 D2）。

## 9. 用量限制

- 统计口径：当前用户在 UTC+08:00 自然日内新建的 `chat_turns` 数量（新的 `request_id` 才算一轮；用同一 `request_id` 重试不重复计数）。失败的轮次也计数（模型已被调用）。
- 上限：用户的 `daily_chat_limit`，为空时取配置项 `LIUHEN_DEFAULT_DAILY_CHAT_LIMIT`（默认 50，D9）。管理员账号不受限制（D16）：检查时 `role = admin` 直接放行，但轮次照常记录，用量统计中仍显示管理员的实际轮数，上限显示为"不限"。管理员的 `daily_chat_limit` 不可设置。
- 超限时 `POST /chat` 返回 429，提示文字"今日对话次数已用完，明天再来"；不影响任务页面的手动操作。
- 检查与新建轮次要在同一事务内完成，避免并发请求同时绕过上限（对该用户行加锁 `SELECT ... FOR UPDATE`，或依赖 `acquire_turn` 现有事务）。
- `GET /auth/me` 返回今日已用/上限，前端在对话页显示。
- 管理员列表中的"近 7 天合计"按 UTC+08:00 的最近 7 个自然日（含今天）统计 `chat_turns`，靠 `(user_id, created_at)` 索引查询，不加统计表（D12）。

## 10. 前端需求

视觉沿用 `frontend/reference/index.html` 原型和设计稿「登录」画板（现有 `LoginPage.vue` 已与之一致）。设计稿没有注册页、修改密码页、个人设置页和管理后台，按登录页的版式扩展（同样的 header、大标题、下划线输入框、主按钮），不新增视觉风格。界面上的品牌名沿用"小记"（D13）。

### 10.1 登录页（`/login`，修改现有页面）

- 接入 `POST /auth/login`，提交时按钮置为加载状态，防止重复提交。
- "保持登录 7 天"复选框启用，对应请求参数 `remember`。
- 错误提示放在现有 `login-notice` 位置，按 5.2 显示不同文案。
- 右上角"注册"跳转 `/register`；"忘记密码"显示"请联系管理员重置密码"。
- 已登录用户访问 `/login` 时跳转首页。
- 登录成功后跳回进入登录页前想访问的地址（`?redirect=`，只允许站内路径）。

### 10.2 注册页（`/register`，新增）

- 字段：用户名、邮箱（可选）、密码、确认密码；前端实时校验规则（5.1）。
- 提交成功后替换为结果说明："注册申请已提交，管理员审核通过后即可登录"，并提供"返回登录"。
- 右上角改为"已有账号？登录"。

### 10.3 修改密码页（`/account/password`，新增）

- 旧密码、新密码、确认新密码。
- 强制改密状态下：页面顶部说明"管理员已重置你的密码，请设置新密码"，路由守卫阻止访问其他页面。

### 10.3.1 个人设置页（`/account`，新增）

- 按 5.8 分区：基本信息与邮箱、修改密码入口、在线设备、登录记录。
- 修改邮箱弹出输入当前密码的确认框。
- 下线设备、"退出其他所有设备"需二次确认；完成后刷新列表。

### 10.4 管理后台（`/admin`，新增，仅管理员可见）

交互原型：`frontend/reference/admin.html`（纯前端模拟数据，直接用浏览器打开）。实现时以原型的布局和交互为准。

- 导航栏只对管理员显示入口，入口上显示待审核数量角标（D15）。
- 列表 + 状态筛选 Tab（待审核 / 正常 / 已停用 / 已拒绝），待审核 Tab 显示数量角标。
- 行内操作：通过、拒绝、禁用、启用、重置密码、调整上限、删除（按状态显示可用操作）。拒绝、禁用、删除需二次确认，可填写原因。
- 重置密码后用弹窗显示临时密码，提供复制按钮，并提示"关闭后无法再次查看"。
- 账号事件列表（可按账号筛选）。

### 10.5 全局行为

- 路由守卫：除 `/login`、`/register` 外都需要登录；应用启动时调用 `GET /auth/me` 确定登录状态。
- `api.ts` 统一处理：401 → 清空用户状态并跳转 `/login?redirect=...`；403 `password_change_required` → 跳转修改密码页；429 → 显示用量提示。
- 请求使用同源 Cookie（`credentials: 'same-origin'`），前端代码中不读写令牌。
- 导航区显示当前用户名，提供"个人设置"、"退出登录"。
- 后端没有的字段仍按 `CLAUDE.md` 规则处理，不把未保存的数据显示为成功。

### 10.6 本地存储按用户隔离

- `localStorage` 中的对话列表键改为按用户区分，如 `liuhen.chats.<user_id>`。
- 登出或检测到当前用户与本地数据所属用户不一致时，清空内存中的 `state`，不显示上一个用户的对话。
- 旧的无用户前缀数据直接丢弃（D17，服务端数据也会重建，旧的 `conversation_id` 已无效）。

## 11. 接口清单

### 11.1 新增

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| POST | `/auth/register` | 公开 | 注册，返回 201，不登录 |
| POST | `/auth/login` | 公开 | `{account, password, remember}`，成功设置 Cookie，返回当前用户 |
| POST | `/auth/logout` | 登录 | 删除当前会话 |
| GET | `/auth/me` | 登录（强制改密时也可用） | 当前用户信息、`must_change_password`、今日用量 |
| POST | `/auth/password` | 登录（强制改密时也可用） | 修改密码 |
| PATCH | `/auth/me` | 登录 | 修改或清空邮箱，`{email, current_password}` |
| GET | `/auth/sessions` | 登录 | 当前用户的在线会话，标记当前会话 |
| DELETE | `/auth/sessions/{id}` | 登录 | 下线自己的某个其他会话；不属于自己 → 404 |
| POST | `/auth/sessions/revoke-others` | 登录 | 退出除当前会话外的所有会话 |
| GET | `/auth/events` | 登录 | 自己最近 30 天的登录与密码相关事件 |
| GET | `/admin/users` | 管理员 | 账号列表，支持 `status`、`q`、分页；含今日与近 7 天对话轮数 |
| GET | `/admin/users/pending-count` | 管理员 | 待审核数量（角标用） |
| POST | `/admin/users/{id}/approve` | 管理员 | 通过审核 |
| POST | `/admin/users/{id}/reject` | 管理员 | 拒绝，可带 `reason` |
| POST | `/admin/users/{id}/disable` | 管理员 | 禁用，可带 `reason` |
| POST | `/admin/users/{id}/enable` | 管理员 | 启用 |
| POST | `/admin/users/{id}/reset-password` | 管理员 | 返回一次性临时密码 |
| PATCH | `/admin/users/{id}` | 管理员 | 修改 `daily_chat_limit` |
| DELETE | `/admin/users/{id}` | 管理员 | 仅限 `pending` / `rejected` |
| GET | `/admin/account-events` | 管理员 | 账号事件分页，可按 `user_id` 筛选 |

状态变更接口用条件更新（`UPDATE ... WHERE id = :id AND status = :expected`），状态不符返回 409，与现有提案状态转换的做法一致。

### 11.2 现有接口的变化

| 接口 | 变化 |
| --- | --- |
| `/tasks/*`、`/actions/*`、`/chat/*`、`/audit/*` | 全部需要登录；只操作当前用户数据；他人资源返回 404 |
| `POST /chat` | 增加用量检查（429）；`request_id`、`conversation_id` 归属校验（6.3） |
| `POST /tasks` | `Idempotency-Key` 归属校验 |
| `GET /actions/pending` | 只返回当前用户的待确认提案 |

错误响应统一为 `{"detail": "文字"}`，文字直接展示给用户，不带错误码（D18）；前端只按 HTTP 状态码做统一处理（401 清空本地用户并跳登录页）。强制改密不靠错误码识别：重置密码会让旧会话失效，用临时密码重新登录后由 `must_change_password` 和路由守卫转到改密页。

## 12. 部署与迁移

### 12.1 Alembic

1. 引入 Alembic，`env.py` 使用 `Base.metadata` 和现有 `database_url` 配置。
2. 生成**基线迁移**，内容等于当前线上表结构；线上库执行 `alembic stamp <基线>` 标记为已在基线版本。
3. 后续迁移脚本完成第 6 节的变更。
4. `main.py` 的 `lifespan` 删除 `Base.metadata.create_all`；启动时检查数据库版本是否为最新，不是则拒绝启动并提示执行 `alembic upgrade head`。
5. 测试（`backend/tests`、`postgres_smoke`、测评的临时库）改为通过 Alembic 建库，或保留测试专用的 `create_all` 辅助函数，二者选一，保证测试库结构与迁移结果一致（增加一个测试比对两者）。

### 12.2 上线顺序（D17：不迁移存量数据）

1. 备份线上数据库（`pg_dump`），保留备份以便需要时人工查阅。
2. 重建数据库（删除并重新创建库，或删除 `postgres_data` 卷后重新初始化）。
3. `alembic upgrade head` 建出全部表。
4. `python -m backend.app.cli create-admin --username <作者>`。
5. 部署新的前后端，修改 Caddyfile（12.3）。
6. 运行 `deploy/verify_site.py`（同步更新为登录后验证）。

### 12.3 Caddy

- 删除 `basic_auth @private` 块和 `LIUHEN_AUTH_USER` / `LIUHEN_AUTH_HASH` 配置（同时清理 `deploy/secrets/auth.env` 相关说明）。
- `@api` 路径增加 `/auth`、`/auth/*`、`/admin`、`/admin/*`。注意前端路由 `/admin` 在 `/app/admin` 下，不与 API 的 `/admin` 冲突。
- 保留现有安全响应头；向后端传 `X-Forwarded-For`。
- 新增配置项：`LIUHEN_COOKIE_SECURE`、`LIUHEN_ALLOWED_ORIGINS`、`LIUHEN_DEFAULT_DAILY_CHAT_LIMIT`，写入 `.env.example`。

## 13. 测试与验收

### 13.1 数据隔离（必须全部通过）

准备用户 A、B，A 有任务、提案、会话、审计事件：

- B 调用 `GET/PATCH/DELETE /tasks/{A的任务}` → 404；`GET /tasks` 看不到 A 的任务。
- B 确认/取消/查看 A 的提案 → 404；`GET /actions/pending` 看不到 A 的提案。
- B 用 A 的 `conversation_id` 发 `/chat` → 404，A 的会话消息数不变。
- B 用 A 用过的 `request_id` 发 `/chat` → 409，响应中不含 A 的回答。
- B 用 A 用过的 `Idempotency-Key` 创建任务 → 409。
- B 删除 A 的会话 → 404。
- B 的审计接口不返回 A 的事件；`/audit/summary` 只统计 B。
- A、B 各自创建时间重叠的任务 → 都成功；同一用户内重叠 → 仍然冲突。
- Agent 工具：以 B 身份调用 `get_task`、`propose_update`、`propose_delete` 传 A 的任务 ID → 返回未找到，不生成提案；工具参数中额外带 `user_id` → 校验失败。
- 路由遍历测试：除白名单外所有路由都依赖 `require_user`。

### 13.2 认证与账号

- 注册后 `pending` 不能登录；通过后可以；拒绝、禁用后不能。
- 密码错误和账号不存在返回相同的状态码和提示文字。
- 禁用后已有会话立即失效（下一次请求 401）。
- 重置密码后旧会话失效；用临时密码登录后只能访问改密相关接口；改密后恢复正常。
- 不勾选"保持登录"的会话空闲超时；勾选的 7 天后过期。
- 登录失败锁定、注册限流生效。
- 非 GET 请求缺少或错误的 `Origin` → 403。
- 管理员不能禁用自己；最后一个管理员不能被禁用。
- 普通用户访问 `/admin/*` → 403。
- 每个账号操作都产生对应的 `account_events`。
- 修改邮箱：当前密码错误 → 拒绝；邮箱已被他人使用 → 拒绝；成功后可用新邮箱登录。
- 在线设备：只列出自己的会话；下线别人的会话 ID → 404；下线后该设备下一次请求 401；"退出其他所有设备"后当前会话仍可用。
- `GET /auth/events` 只返回自己的事件，且只含 5.8 列出的类型。

### 13.3 用量

- 管理员超过默认上限后 `/chat` 仍正常；轮次照常计入统计。
- 达到上限后 `/chat` 返回 429；同一 `request_id` 重试不计数；跨自然日（UTC+08:00）重置；管理员调整上限立即生效；并发请求不能突破上限。

### 13.4 测评

- 测评运行器（`backend/app/evals/run.py`）在隔离库中创建一个测试用户，所有场景以该用户身份执行；21 条场景的业务判定结果与改造前一致。
- 新增至少 1 条跨用户场景：库中预置另一个用户的同名任务，Agent 不能读到或修改它。
- 以上两项在多用户需求全部完成、调整测评集之后再做（D19），不作为 P2–P5 单个阶段的完成条件。

### 13.5 前端

- `npm run build` 通过。
- 手动验证：登录、登出、注册、待审核提示、强制改密、个人设置（改邮箱、下线设备、登录记录）、管理员审核/禁用/重置与待审核角标、切换账号后看不到上一个账号的本地对话。

## 14. 开发阶段划分

每个阶段在特性分支上完成，结束时按 `CLAUDE.md` 的"阶段完成"流程清理并合入 `main`。

| 阶段 | 内容 | 完成标准 |
| --- | --- | --- |
| P1 迁移基础（已完成） | 引入 Alembic、基线迁移、测试建库方式调整 | 现有测试全部通过；线上库可 `stamp` |
| P2 数据隔离（已完成） | 业务表加 `user_id`、排他约束按用户、服务/仓储/工具全链路传 `user_id`、`request_id` 等归属校验；此阶段请求固定归属开发用户 `dev`（`api/deps.py`，标 `TEMP`） | 13.1 全部通过（`test_user_isolation.py`）；跨用户测评场景与真实模型测评回归后置（D19） |
| P3 认证会话（已完成） | 密码哈希、`user_sessions`、`/auth/login\|logout\|me\|password`、`CurrentUserId` 改为从会话解析、Origin 校验、限流、CLI；前端登录页接入、改密页（因 CLI 的重置密码在本阶段可用，从 P4 提前）、路由守卫、`localStorage` 隔离 | 13.2 中登录/会话部分通过（`test_auth.py`）；P2 的 `TEMP` 注入已删除 |
| P4 注册审核与账号管理 | 注册、状态机、`/admin/*`、`account_events`、个人设置（邮箱、在线设备、登录记录）；`users` 增加 `reviewed_at`、`reviewed_by`；前端注册页、个人设置页、管理后台 | 13.2 全部通过 |
| P5 用量与上线 | 每日对话上限与管理员用量统计、Caddy 改造、`verify_site.py` 更新、存量数据迁移演练、文档更新（`CLAUDE.md`、各 README） | 13.3、13.5 通过；在线上库的副本上完整演练 12.2 |

## 15. 讨论记录

### 15.1 已确认（2026-11-21）

| 问题 | 结论 | 决策 |
| --- | --- | --- |
| 每日对话默认上限 | 50 轮 | D9 |
| 用户能否查看自己的登录记录 | 可以，并且可以下线其他设备 | D10 |
| 用户能否自助注销账号 | 第一期不做 | D11 |
| 管理员用量统计粒度 | 今日 + 近 7 天合计 | D12 |
| 登录页品牌名 | 沿用"小记" | D13 |
| 用户名/邮箱能否修改 | 用户名不可改，邮箱可改（需当前密码） | D14 |
| 审核提醒方式 | 只在界面显示角标 | D15 |
| 错误响应要不要带错误码 | 不要，只返回给用户看的文字；P3 已有的认证类错误码已去掉 | D18 |
| 跨用户测评场景与真实模型回归何时做 | 多用户需求完成、调整测评集之后 | D19 |

### 15.2 待讨论

暂无。
