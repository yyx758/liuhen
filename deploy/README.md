# 留痕部署

此目录的 Compose 配置面向 `liuhen.yyyyx.top`：Caddy 提供 HTTPS、公开的备案入口页、`/app/` 前端页面，并把 API 路径转发给 FastAPI（路径前缀逐个列在 `Caddyfile` 的 `@api` 里，新增接口前缀时要同步添加，如资料库的 `/documents`）；FastAPI 和 PostgreSQL 仅在 Compose 内部网络开放。访问控制由应用自己的账号登录负责（注册需管理员审核），网关不再做 HTTP Basic 登录。业务数据保存在 `postgres_data` 卷，Caddy 证书保存在 `caddy_data` 卷。

## 首次部署

1. 为 `liuhen.yyyyx.top` 添加指向服务器的 A 记录。首次签发证书时使用 Cloudflare「仅 DNS」模式，并确认服务器的 80/443 端口可从公网访问。
2. 在服务器的 `deploy/secrets/` 中创建 `model.env`，仅包含 `LIUHEN_BASE_URL`、`LIUHEN_API_KEY`、`LIUHEN_MODEL`。运行 `python3 deploy/setup_postgres.py` 生成私有的 `postgres.env`。再创建 `smtp.env`，包含 `LIUHEN_SMTP_HOST=smtp.qq.com`、`LIUHEN_SMTP_PORT=465`、`LIUHEN_SMTP_USERNAME`（完整发件邮箱）、`LIUHEN_SMTP_PASSWORD`（QQ 邮箱 SMTP 授权码，不是 QQ 密码），权限设为 600。
3. 检查 `docker compose -f deploy/compose.yaml config --quiet`。这个命令需要上述三个环境文件存在。
4. 构建镜像并建表：`docker compose -f deploy/compose.yaml build --build-arg PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple api web`，然后 `docker compose -f deploy/compose.yaml run --rm api alembic -c backend/alembic.ini upgrade head`。服务器在国内时，构建容器里直连 PyPI 很慢，会超时报"找不到版本"，需用 `PIP_INDEX_URL` 指定镜像源。
5. 创建管理员：`docker compose -f deploy/compose.yaml run --rm api python -m backend.app.cli create-admin --username <名字>`（交互输入密码）。
6. 启动 `db`、`api` 和 `web`。PostgreSQL 健康后 API 才会启动；数据库结构版本不是最新时 API 拒绝启动。Caddy 自动申请并续期证书。

`model.env`、`postgres.env`、`smtp.env` 被 Git 和 Docker 构建上下文忽略，不得提交。服务器需要能访问 `smtp.qq.com:465`（部分云厂商默认封禁出站 25 端口，465 一般不受影响）。

## 配置

`compose.yaml` 中 API 的环境变量：

| 变量 | 说明 |
| --- | --- |
| `LIUHEN_ALLOWED_ORIGINS` | 允许发起写请求的页面来源（Origin 校验），线上为 `https://liuhen.yyyyx.top` |
| `LIUHEN_COOKIE_SECURE` | 会话 Cookie 是否带 `Secure`，线上必须为 `true` |
| `LIUHEN_DEFAULT_DAILY_CHAT_LIMIT` | 普通用户每日对话轮数默认上限（默认 50）；管理员不限量，可在管理后台为单个用户调整 |
| `FORWARDED_ALLOW_IPS` | uvicorn 信任的代理地址。API 只在内部网络可达，设为 `*`，登录与注册限流才能按 Caddy 传来的真实 IP 计算 |

## 多用户版本上线（已于 2026-11-22 完成）

多用户改造不迁移旧数据（D17），迁移 `0003` 在业务表已有数据时会拒绝执行；当时线上库建于引入 Alembic 之前，没有 `alembic_version` 表。上线按"备份并验证 → 停服务 → 删除 `postgres_data` 卷重建空库 → 迁移 → 创建管理员 → 启动"完成。单用户时期的数据只保存在服务器的 `/opt/liuhen/backups/liuhen-before-multi-user.dump` 中，需要时用 `pg_restore -l` 查阅目录，或恢复到临时库查看。

## 访问与维护

- 公开入口：`https://liuhen.yyyyx.top/`，显示项目简介和 ICP 备案号。`/app/` 进入应用，未登录时跳转到登录页；新用户在登录页点"注册"提交申请，管理员在"管理"页面审核通过后才能登录。
- 管理员忘记密码：`docker compose -f deploy/compose.yaml run --rm api python -m backend.app.cli reset-password --username <名字>`，临时密码只显示一次。
- 该配置只服务 `liuhen.yyyyx.top`，不处理根域名和 `www`。
- 上线前检查：后端测试通过；本次上线若包含影响 Agent 行为的改动（提示词、Agent 循环、工具、模型等，范围见根目录 `CLAUDE.md`"测试与验证"），全量测评须达到 `docs/项目进度.md` 中的最新基线。
- 更新：服务器上的代码不是 Git 仓库，用 `git archive` 上传已推送的 `main`。先解压到新目录并复制 `deploy/secrets/`、`backups/`，构建镜像（构建期间旧容器照常运行），停服务后再对调目录，旧目录保留到确认无误（下次更新前先删掉上一次留下的 `/opt/liuhen.old`）：
  ```bash
  # 本地执行：git archive --format=tar main | ssh <服务器> 'sudo mkdir /opt/liuhen.new && sudo chown ubuntu:ubuntu /opt/liuhen.new && tar -xf - -C /opt/liuhen.new'
  cd /opt/liuhen.new && cp -a /opt/liuhen/deploy/secrets deploy/ && cp -a /opt/liuhen/backups .
  docker compose -f deploy/compose.yaml build --build-arg PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple api web
  cd /opt/liuhen && docker compose -f deploy/compose.yaml down
  sudo mv /opt/liuhen /opt/liuhen.old && sudo mv /opt/liuhen.new /opt/liuhen && cd /opt/liuhen
  docker compose -f deploy/compose.yaml run --rm api alembic -c backend/alembic.ini upgrade head
  docker compose -f deploy/compose.yaml up -d
  ```
  执行有结构变更的迁移前先备份。
- 查看状态：`docker compose -f deploy/compose.yaml ps`；查看日志：`docker compose -f deploy/compose.yaml logs --tail=100`。
- 站点检查：`python3 deploy/verify_site.py` 检查 HTTPS、备案页、应用页面与接口的登录保护。临时提供 `LIUHEN_VERIFY_USER` 和 `LIUHEN_VERIFY_PASSWORD` 时还会登录检查业务接口并在结束时退出登录（会留下一条登录记录）。
- 备份：在 `/opt/liuhen` 执行 `docker compose -f deploy/compose.yaml exec -T db pg_dump -U liuhen -Fc liuhen > backups/<名字>.dump`，再 `chmod 600`。必须带 `-T`：`exec` 默认分配伪终端，二进制格式的备份经过终端可能被改写而损坏，导出时不报错，恢复时才发现。备份后用 `pg_restore -l` 确认能读出目录。不要把备份或模型密钥提交到 Git。
- 登录与注册限流在 API 进程内计数，适用于单实例部署；扩展为多个 API 实例前需要改为共享存储。
