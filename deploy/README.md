# 留痕部署

此目录的 Compose 配置面向 `liuhen.yyyyx.top`：Caddy 提供 HTTPS、公开的备案入口页、受单用户 HTTP Basic 登录保护的 `/app/` 和 API；FastAPI 仅在 Compose 内部网络开放。SQLite 数据保存在 `liuhen_data` 卷，Caddy 证书保存在 `caddy_data` 卷。

## 上线前

1. 为 `liuhen.yyyyx.top` 添加指向服务器的 A 记录。首次签发证书时使用 Cloudflare「仅 DNS」模式，并确认服务器的 80/443 端口可从公网访问。
2. 在服务器的 `deploy/secrets/` 中创建 `model.env`，仅包含 `LIUHEN_BASE_URL`、`LIUHEN_API_KEY`、`LIUHEN_MODEL`。运行 `python3 deploy/setup_auth.py` 生成单用户 `auth.env` 和一次性登录凭据。脚本通过 Caddy 哈希密码，明文只保存在服务器上权限为 600 的 `deploy/secrets/login-once.txt`；保存密码后删除该文件。
3. 检查 `docker compose -f deploy/compose.yaml config --quiet`。这个命令需要上述两个文件存在。
4. 先构建并启动 `api`，确认健康后，再切换占用 80/443 的旧 Web 服务并启动 `web`。Caddy 自动申请并续期证书。

`model.env` 和 `auth.env` 被 Git 和 Docker 构建上下文忽略，不得提交。服务器初次生成的登录密码应只通过 SSH 获取并妥善保存。

## 访问与维护

- 公开入口：`https://liuhen.yyyyx.top/`，显示项目简介和 ICP 备案号。点击进入 `/app/` 后浏览器会要求登录；未登录访问应用和 API 返回 401。
- `yyyyx.top` 和 `www.yyyyx.top` 的 HTTPS 入口跳转到新站点。首次申请这两个域名的证书时，先将对应 DNS 记录改为「仅 DNS」；证书成功签发后可恢复 Cloudflare 代理，并使用 Full (strict) 模式。
- 更新：上传新代码后，在服务器运行 `docker compose -f deploy/compose.yaml up -d --build`。
- 查看状态：`docker compose -f deploy/compose.yaml ps`；查看日志：`docker compose -f deploy/compose.yaml logs --tail=100`。
- 备份：定期备份 `liuhen_data` 卷中的 SQLite 数据库。不要把数据库文件或模型密钥提交到 Git。

当前登录保护由网关统一执行，应用本身还没有账号和数据归属机制，适用于单人使用。要支持多人使用，需要在后端增加用户身份与任务、对话、提案的归属校验。
