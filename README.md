# 留痕（liuhen）

留痕是一个面向个人任务管理的 AI Agent 项目。用户可以用自然语言创建和查询任务；Agent 对任务的修改、删除会生成提案，待用户确认后执行。后端保存对话、工具调用结果和业务操作审计记录，方便核对实际执行结果。

这是项目的初版，优先实现任务管理、操作留痕和可验证的执行流程。

## 当前能力

- **任务管理**：创建、查询、手动编辑和删除任务；区分开始时间、截止时间与可选时长。
- **AI 对话**：将自然语言请求交给 Agent，调用已注册的任务工具；修改和删除由用户确认提案。
- **操作留痕**：保存对话和工具结果，业务变更写入审计事件；前端留痕页支持筛选和分页。
- **前端页面**：Vue 3 实现对话、待办、日历和留痕页面；助手回复支持 Markdown 渲染。
- **账号**：多用户数据隔离；注册后由管理员审核；账号密码登录、个人设置（邮箱、在线设备、登录记录）；管理员后台可审核、停用、重置密码并查看账号事件。
- **验证**：后端自动化测试和独立数据库的 Agent 测评场景。

提醒消息的主动推送、文档管理后端及 Agent 工具尚未实现。前端资料库目前展示原型示例，不能保存或检索文档。每日对话上限与用量统计尚未实现。

## 本地启动

需要 Python 3.11 或更新版本，以及 Node.js 和 npm。以下命令在项目根目录执行（PowerShell）：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend/requirements.txt
Copy-Item .env.example .env
```

在 `.env` 中填写模型接口的 `base_url`、`api_key`、`model` 和 PostgreSQL `database_url`。本地需先启动 PostgreSQL；配置方式见[后端说明](backend/README.md)。`.env` 已被 Git 忽略，不要提交真实密钥。

启动后端：

```powershell
python -m backend.app.main
```

另开终端启动前端：

```powershell
cd frontend/app
npm ci
npm run dev
```

前端地址为 <http://127.0.0.1:5173/>，API 文档为 <http://127.0.0.1:8000/docs>。

## 项目结构

| 目录 | 内容 |
| --- | --- |
| `backend/app/` | FastAPI 路由、Agent、业务服务与数据库模型 |
| `backend/tests/` | 后端自动化测试 |
| `frontend/app/` | Vue 3 前端应用 |
| `frontend/reference/` | 前端视觉原型 |

更详细的实现、接口与测试说明见[后端 README](backend/README.md)和[前端 README](frontend/app/README.md)。

## 部署

单人使用的 Docker Compose 部署方案见[部署说明](deploy/README.md)。公网入口由 Caddy 提供 HTTPS 和登录保护；后端不直接暴露到公网。
