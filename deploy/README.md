# 留痕部署

此目录的 Compose 配置面向 `liuhen.yyyyx.top`：Caddy 提供 HTTPS、公开的备案入口页、受单用户 HTTP Basic 登录保护的 `/app/` 和 API；FastAPI 和 PostgreSQL 仅在 Compose 内部网络开放。业务数据保存在 `postgres_data` 卷，Caddy 证书保存在 `caddy_data` 卷。

## 上线前

1. 为 `liuhen.yyyyx.top` 添加指向服务器的 A 记录。首次签发证书时使用 Cloudflare「仅 DNS」模式，并确认服务器的 80/443 端口可从公网访问。
2. 在服务器的 `deploy/secrets/` 中创建 `model.env`，仅包含 `LIUHEN_BASE_URL`、`LIUHEN_API_KEY`、`LIUHEN_MODEL`。运行 `python3 deploy/setup_postgres.py` 生成私有的 `postgres.env`；运行 `python3 deploy/setup_auth.py` 生成单用户 `auth.env` 和一次性登录凭据。明文登录密码只保存在权限为 600 的 `deploy/secrets/login-once.txt`；保存后删除该文件。
3. 检查 `docker compose -f deploy/compose.yaml config --quiet`。这个命令需要上述三个环境文件存在。
4. 建表：`docker compose -f deploy/compose.yaml run --rm api alembic -c backend/alembic.ini upgrade head`。
5. 创建管理员：`docker compose -f deploy/compose.yaml run --rm api python -m backend.app.cli create-admin --username <名字>`（交互输入密码）。
6. 启动 `db`、`api` 和 `web`。PostgreSQL 健康后 API 才会启动；数据库结构版本不是最新时 API 拒绝启动。Caddy 自动申请并续期证书。

`model.env`、`postgres.env` 和 `auth.env` 被 Git 和 Docker 构建上下文忽略，不得提交。服务器初次生成的登录密码应只通过 SSH 获取并妥善保存。

## 访问与维护

- 公开入口：`https://liuhen.yyyyx.top/`，显示项目简介和 ICP 备案号。点击进入 `/app/` 后浏览器会要求登录；未登录访问应用和 API 返回 401。
- 该配置只服务 `liuhen.yyyyx.top`，不处理根域名和 `www`。
- 更新：上传新代码后，先构建镜像并执行迁移，再启动：
  ```bash
  docker compose -f deploy/compose.yaml build api web
  docker compose -f deploy/compose.yaml run --rm api alembic -c backend/alembic.ini upgrade head
  docker compose -f deploy/compose.yaml up -d
  ```
  执行有结构变更的迁移前先用 `pg_dump` 备份。
- 引入 Alembic 前建出的线上库（只需做一次）：先备份，再查看是否已有 `users` 表（`docker compose -f deploy/compose.yaml exec db psql -U <用户> -d <库> -c '\dt users'`）。没有则 `stamp 0001`，已有则 `stamp 0002`（命令同上，把 `upgrade head` 换成 `stamp 0001` 或 `stamp 0002`），之后按正常更新流程执行 `upgrade head`。
- 查看状态：`docker compose -f deploy/compose.yaml ps`；查看日志：`docker compose -f deploy/compose.yaml logs --tail=100`。
- 站点检查：`python3 deploy/verify_site.py`。若要同时检查登录后的应用和 API，可临时提供 `LIUHEN_VERIFY_AUTH_USER` 和 `LIUHEN_VERIFY_AUTH_PASSWORD` 环境变量；不设置时只检查公开页面和未登录保护。
- 备份：定期使用 `pg_dump` 备份 PostgreSQL。不要把备份或模型密钥提交到 Git。

当前登录保护由网关统一执行，应用本身还没有账号和数据归属机制，适用于单人使用。要支持多人使用，需要在后端增加用户身份与任务、对话、提案的归属校验。
