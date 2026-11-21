# 留痕部署

此目录的 Compose 配置面向 `liuhen.yyyyx.top`：Caddy 提供 HTTPS、公开的备案入口页、`/app/` 前端页面，并把 API 路径转发给 FastAPI；FastAPI 和 PostgreSQL 仅在 Compose 内部网络开放。访问控制由应用自己的账号登录负责（注册需管理员审核），网关不再做 HTTP Basic 登录。业务数据保存在 `postgres_data` 卷，Caddy 证书保存在 `caddy_data` 卷。

## 首次部署

1. 为 `liuhen.yyyyx.top` 添加指向服务器的 A 记录。首次签发证书时使用 Cloudflare「仅 DNS」模式，并确认服务器的 80/443 端口可从公网访问。
2. 在服务器的 `deploy/secrets/` 中创建 `model.env`，仅包含 `LIUHEN_BASE_URL`、`LIUHEN_API_KEY`、`LIUHEN_MODEL`。运行 `python3 deploy/setup_postgres.py` 生成私有的 `postgres.env`。
3. 检查 `docker compose -f deploy/compose.yaml config --quiet`。这个命令需要上述两个环境文件存在。
4. 建表：`docker compose -f deploy/compose.yaml run --rm api alembic -c backend/alembic.ini upgrade head`。
5. 创建管理员：`docker compose -f deploy/compose.yaml run --rm api python -m backend.app.cli create-admin --username <名字>`（交互输入密码）。
6. 启动 `db`、`api` 和 `web`。PostgreSQL 健康后 API 才会启动；数据库结构版本不是最新时 API 拒绝启动。Caddy 自动申请并续期证书。

`model.env`、`postgres.env` 被 Git 和 Docker 构建上下文忽略，不得提交。

## 配置

`compose.yaml` 中 API 的环境变量：

| 变量 | 说明 |
| --- | --- |
| `LIUHEN_ALLOWED_ORIGINS` | 允许发起写请求的页面来源（Origin 校验），线上为 `https://liuhen.yyyyx.top` |
| `LIUHEN_COOKIE_SECURE` | 会话 Cookie 是否带 `Secure`，线上必须为 `true` |
| `LIUHEN_DEFAULT_DAILY_CHAT_LIMIT` | 普通用户每日对话轮数默认上限（默认 50）；管理员不限量，可在管理后台为单个用户调整 |
| `FORWARDED_ALLOW_IPS` | uvicorn 信任的代理地址。API 只在内部网络可达，设为 `*`，登录与注册限流才能按 Caddy 传来的真实 IP 计算 |

## 多用户版本上线（一次性，D17：不迁移存量数据）

多用户改造不迁移旧数据，迁移 `0003` 在业务表已有数据时会拒绝执行。从单用户版本升级时按以下顺序操作，先在线上库的副本上完整演练一遍：

1. 备份：`docker compose -f deploy/compose.yaml exec db pg_dump -U <用户> -Fc <库> > liuhen-before-multi-user.dump`，妥善保存，需要时可用 `pg_restore -l` 查阅。
2. 停止服务并重建数据库：`docker compose -f deploy/compose.yaml down`，删除 `postgres_data` 卷（`docker volume rm liuhen_postgres_data`），再 `docker compose -f deploy/compose.yaml up -d db` 重新初始化。
3. 构建镜像并建表：`docker compose -f deploy/compose.yaml build api web`，然后 `docker compose -f deploy/compose.yaml run --rm api alembic -c backend/alembic.ini upgrade head`。
4. 创建管理员（见"首次部署"第 5 步）。
5. `docker compose -f deploy/compose.yaml up -d` 启动新的前后端（Caddyfile 已去掉 Basic Auth、增加 `/admin` 转发）。旧的 `deploy/secrets/auth.env` 不再使用，可以删除。
6. 运行站点检查（见下文），并用管理员账号登录 `/app/` 确认能看到"管理"入口。

## 访问与维护

- 公开入口：`https://liuhen.yyyyx.top/`，显示项目简介和 ICP 备案号。`/app/` 进入应用，未登录时跳转到登录页；新用户在登录页点"注册"提交申请，管理员在"管理"页面审核通过后才能登录。
- 管理员忘记密码：`docker compose -f deploy/compose.yaml run --rm api python -m backend.app.cli reset-password --username <名字>`，临时密码只显示一次。
- 该配置只服务 `liuhen.yyyyx.top`，不处理根域名和 `www`。
- 更新：上传新代码后，先构建镜像并执行迁移，再启动：
  ```bash
  docker compose -f deploy/compose.yaml build api web
  docker compose -f deploy/compose.yaml run --rm api alembic -c backend/alembic.ini upgrade head
  docker compose -f deploy/compose.yaml up -d
  ```
  执行有结构变更的迁移前先用 `pg_dump` 备份。
- 查看状态：`docker compose -f deploy/compose.yaml ps`；查看日志：`docker compose -f deploy/compose.yaml logs --tail=100`。
- 站点检查：`python3 deploy/verify_site.py` 检查 HTTPS、备案页、应用页面与接口的登录保护。临时提供 `LIUHEN_VERIFY_USER` 和 `LIUHEN_VERIFY_PASSWORD` 时还会登录检查业务接口并在结束时退出登录（会留下一条登录记录）。
- 备份：定期使用 `pg_dump` 备份 PostgreSQL。不要把备份或模型密钥提交到 Git。
- 登录与注册限流在 API 进程内计数，适用于单实例部署；扩展为多个 API 实例前需要改为共享存储。
