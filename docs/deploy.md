# 部署到云服务器（exam.fufu235.top）

这份指南带你把 project-b 部署到腾讯云香港轻量服务器（124.156.172.59，2 核 2 GB，Ubuntu 22.04），用 `https://exam.fufu235.top` 访问。
命令都在两个地方运行，每一步都标明了：

- **【Windows】**：你电脑上的 PowerShell（开始菜单搜 "PowerShell"）。
- **【服务器】**：SSH 登录到服务器之后的终端。

> 安全提醒：服务器密码、`.env` 里的密钥、GitHub Client Secret 都只在你自己的电脑和服务器上输入，不要发到任何聊天里。

## 生产环境和本机开发有什么不同

| | 本机开发 | 生产环境 |
| --- | --- | --- |
| 访问方式 | http://localhost:8090 | https://exam.fufu235.top |
| HTTPS | 无 | Caddy 自动申请和续期 Let's Encrypt 证书 |
| 登录 | 开发登录（任意用户名） | 只能用 GitHub 登录，开发登录强制关闭 |
| 对外端口 | 8090、8000、5433、6380 | 只有 80 和 443（Caddy），数据库、Redis、API 都不对外 |
| 密钥 | 默认值 | `.env` 里的随机值；JWT 密钥太弱时后端拒绝启动 |
| 内存 | 不限制 | Postgres、Redis、worker 都限制了内存，另加 2 GB swap |

相关文件：

- `docker-compose.prod.yml`：叠加在 `docker-compose.yml` 上的生产覆盖（关端口、加 Caddy、限内存、强制 `ENV=prod`）。
- `deploy/Caddyfile`：HTTPS 反向代理配置。
- `deploy/env.prod.example`：生产 `.env` 模板。
- `deploy/setup-swap.sh`：加 2 GB swap。
- `deploy/deploy.sh`：一键构建并启动，以后更新代码也用它。

请求的路径是：浏览器 → Caddy（443，HTTPS）→ frontend 的 Nginx（静态页面；`/api` 转发）→ api（FastAPI）。

---

## 第 0 步：准备和检查

### 0.1 放行防火墙端口

腾讯云控制台 → 轻量应用服务器 → 你的服务器 → **防火墙**，确认有这三条规则（没有就添加）：

| 端口 | 协议 | 用途 |
| --- | --- | --- |
| 22 | TCP | SSH 登录 |
| 80 | TCP | 申请证书、http 跳转到 https |
| 443 | TCP | 网站访问 |

**不要**放行 5433、6380、8000、8090。

### 0.2 检查域名解析

**【Windows】**

```powershell
nslookup exam.fufu235.top 1.1.1.1
```

`Address:` 后面应该是你服务器的公网 IP。如果显示的是别的地址（比如 104.x、172.x 开头的 Cloudflare 地址），说明 Cloudflare 上那条 A 记录是橙色云朵，要点成灰色（"仅 DNS"），否则 Caddy 申请不到证书。

香港服务器不需要 ICP 备案，解析好就能直接访问。

---

## 第 1 步：SSH 登录服务器

腾讯云的 Ubuntu 镜像默认用户是 `ubuntu`，不能用 root 加密码登录。推荐用 SSH 密钥登录，以后 `ssh` 和 `scp` 都不用输密码。

**1.1 在电脑上生成密钥【Windows】**（只做一次，三个问题都直接回车）

```powershell
ssh-keygen -t ed25519
type $env:USERPROFILE\.ssh\id_ed25519.pub
```

复制输出的那一行 `ssh-ed25519 AAAA...`。这是公钥，可以公开；不带 `.pub` 的 `id_ed25519` 是私钥，不要给任何人。

**1.2 把公钥放到服务器上**：控制台 → 服务器 → **登录**（网页终端），运行（把引号里换成你的公钥）：

```bash
sudo -i
mkdir -p /home/ubuntu/.ssh
echo "ssh-ed25519 AAAA...你的公钥..." >> /home/ubuntu/.ssh/authorized_keys
chown -R ubuntu:ubuntu /home/ubuntu/.ssh
chmod 700 /home/ubuntu/.ssh && chmod 600 /home/ubuntu/.ssh/authorized_keys
```

**1.3 登录【Windows】**

```powershell
ssh ubuntu@124.156.172.59
```

第一次会问 `Are you sure you want to continue connecting`，输入 `yes`。

**1.4 每次登录后先切换到 root，再进项目目录【服务器】**

```bash
sudo -i
cd /opt/project-b
```

> 后面所有【服务器】命令都要求：提示符以 `#` 结尾（root），并且当前目录是 `/opt/project-b`。
> 常见错误：`permission denied` = 还没 `sudo -i`；`no configuration file provided` 或 nano 打开的是空的 `New File` = 没 `cd /opt/project-b`（`sudo -i` 会回到 `/root`）。

---

## 第 2 步：安装 Docker

**【服务器】**先看看有没有装：

```bash
docker --version
docker compose version
```

- 两条都有输出，并且 `docker compose version` 的版本号 **≥ 2.24**：跳到第 3 步。
- 提示 `command not found` 或版本太低：安装最新版。

```bash
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
docker compose version
```

---

## 第 3 步：把代码传到服务器

代码包已经放在你电脑上：`D:\Projects\实习\project-b\dist-deploy\project-b.tar.gz`（不含 `.env` 和 `.git`）。

**【Windows】**（新开一个 PowerShell 窗口，不要在 SSH 窗口里运行）

```powershell
cd "D:\Projects\实习\project-b\dist-deploy"
scp project-b.tar.gz ubuntu@124.156.172.59:/home/ubuntu/
```

没配密钥的话，也可以用腾讯云网页终端顶部的"上传文件"按钮上传。

**【服务器】**

```bash
mkdir -p /opt/project-b
tar xzf /home/ubuntu/project-b.tar.gz -C /opt/project-b
cd /opt/project-b
ls
```

能看到 `backend`、`frontend`、`deploy`、`docker-compose.prod.yml` 就对了。之后的【服务器】命令都在 `/opt/project-b` 目录下运行（重新登录后先 `cd /opt/project-b`）。

---

## 第 4 步：加 2 GB swap

**【服务器】**

```bash
bash deploy/setup-swap.sh
```

最后打印的 `free -h` 里，`Swap:` 一行应该是 `2.0Gi`。为什么要加：构建前端镜像和分析大仓库时，内存峰值可能超过 2 GB，没有 swap 时进程会被系统直接杀掉。

---

## 第 5 步：创建 GitHub OAuth App

浏览器打开 https://github.com/settings/developers → **OAuth Apps** → **New OAuth App**，填：

| 字段 | 填写 |
| --- | --- |
| Application name | project-b（随意） |
| Homepage URL | `https://exam.fufu235.top` |
| Authorization callback URL | `https://exam.fufu235.top/api/auth/github/callback` |

点 **Register application** 后：

1. 复制页面上的 **Client ID**。
2. 点 **Generate a new client secret**，复制生成的 Secret（**只显示这一次**，先存到记事本里）。

> 如果之前为本机开发建过一个回调地址是 localhost 的 OAuth App，不要改它，给生产单独建一个。一个 OAuth App 只能填一个回调地址。

---

## 第 6 步：填写 .env

**【服务器】**

```bash
cp deploy/env.prod.example .env
chmod 600 .env

# 自动生成数据库密码和 JWT 密钥（随机值，不需要你记住）
sed -i "s|^POSTGRES_PASSWORD=CHANGE_ME|POSTGRES_PASSWORD=$(openssl rand -hex 24)|" .env
sed -i "s|^JWT_SECRET=CHANGE_ME|JWT_SECRET=$(openssl rand -hex 32)|" .env

nano .env
```

在 nano 里用方向键移动，把剩下的三个 `CHANGE_ME` 换掉：

| 变量 | 填写 |
| --- | --- |
| `GITHUB_CLIENT_ID` | 第 5 步的 Client ID |
| `GITHUB_CLIENT_SECRET` | 第 5 步的 Client Secret |
| `ADMIN_GITHUB_LOGINS` | 你的 GitHub 用户名（能看到运维页），多个用逗号隔开 |

保存退出：`Ctrl+O` → 回车 → `Ctrl+X`。

检查一下没有遗漏（没有输出就对了）：

```bash
grep "=CHANGE_ME" .env
```

模型先保持 `LLM_PROVIDER=fake`，部署跑通后再按第 9 步接真实模型。

> 数据库密码第一次启动时就写进了数据库。以后再改 `.env` 里的 `POSTGRES_PASSWORD`，api 会连不上数据库。上线后不要改它。

---

## 第 7 步：构建并启动

**【服务器】**

```bash
bash deploy/deploy.sh
```

第一次要下载基础镜像、安装依赖、构建前端，大约 5～15 分钟。脚本会逐个构建镜像，避免 2 GB 内存同时构建两个镜像时爆内存。结束时会打印 `docker compose ps` 的结果。

---

## 第 8 步：验证

**【服务器】**

```bash
docker compose ps
```

`api`、`worker`、`frontend`、`postgres`、`redis` 的 STATUS 都应该是 `Up ... (healthy)`，`caddy` 是 `Up`，`migrate` 是 `Exited (0)`（它只运行一次迁移，退出码 0 表示成功）。刚启动时可能显示 `(health: starting)`，等半分钟再看。

查看证书申请情况：

```bash
docker compose logs caddy | grep -iE "certificate obtained|error"
```

看到 `certificate obtained successfully` 就表示 HTTPS 证书已经拿到。

测试接口：

```bash
curl -s https://exam.fufu235.top/api/health
```

应该返回一段 JSON（包含 `ok`）。

**【浏览器】**

1. 打开 https://exam.fufu235.top ，地址栏有小锁。
2. 登录页只有"用 GitHub 登录"，**没有**开发模式登录。
3. 点 GitHub 登录 → 授权 → 回到首页并显示你的用户名。
4. 你是管理员，侧边栏能看到运维页。
5. 提交 https://github.com/spring-projects/spring-petclinic ，能分析完成（假模型下出的题是模板化的，正常）。

全部通过就部署完成了。

---

## 第 9 步（可选）：接入真实模型

**【服务器】** `nano .env`，修改：

```
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.deepseek.com/v1      # 你的服务商地址
LLM_API_KEY=你的 Key
LLM_MODEL_STRONG=...
LLM_MODEL_FAST=...
EMBEDDING_MODEL=...
EMBEDDING_DIM=...                              # 必须和嵌入模型的维度一致
LLM_PRICE_TABLE={"模型名": {"input": 价格, "output": 价格}}
```

然后让 api 和 worker 重新读取配置：

```bash
docker compose up -d --force-recreate api worker
```

**注意 `EMBEDDING_DIM`**：数据库的向量列是按第一次迁移时的维度建的。如果新嵌入模型的维度不是 256，要清空数据库重建（**会删掉所有用户、仓库和考核记录**）：

```bash
docker compose down
docker volume rm project-b_pgdata
bash deploy/deploy.sh
```

所以最好在正式有人使用之前就把模型定下来。

---

## 以后更新代码

1. 让 Claude 重新打包，或者自己在项目目录里运行 `git archive --format=tar.gz -o dist-deploy/project-b.tar.gz HEAD`。
2. 按第 3 步上传并解压到 `/opt/project-b`（`.env` 不在包里，不会被覆盖）。
3. **【服务器】** `cd /opt/project-b && bash deploy/deploy.sh`

数据库迁移会在启动时自动执行。

## 常用运维命令

**【服务器】**（都在 `/opt/project-b` 下运行）

```bash
docker compose ps                         # 各服务状态
docker compose logs -f --tail 100 api     # 实时看 api 日志（Ctrl+C 退出）；换成 worker / caddy 看别的
docker compose restart worker             # 重启某个服务
docker compose down                       # 停止全部（数据保留在卷里）
docker compose up -d                      # 重新启动
free -h && df -h /                        # 内存和磁盘
docker stats --no-stream                  # 每个容器占多少内存

# 备份数据库到当前目录
docker compose exec -T postgres pg_dump -U projectb projectb | gzip > backup-$(date +%F).sql.gz
```

备份文件可以用 `scp ubuntu@124.156.172.59:/home/ubuntu/backup-*.sql.gz .（先在服务器上 `cp backup-*.sql.gz /home/ubuntu/`）` 下载到电脑上（在【Windows】运行）。

## 常见问题

**打开网站提示证书错误，或者 caddy 日志里有 `challenge failed`**

- `nslookup` 解析出来的不是服务器 IP：Cloudflare 改成灰云，等几分钟。
- 防火墙没放行 80 或 443：回到第 0.1 步。
- 失败太多次会被 Let's Encrypt 暂时限流，修好之后 `docker compose restart caddy`，等 10 分钟左右再试。

**GitHub 登录后显示 `redirect_uri is not associated with this application`**

OAuth App 的回调地址和 `.env` 里的 `OAUTH_REDIRECT_URL` 不一致。两边都必须是 `https://exam.fufu235.top/api/auth/github/callback`，注意是 https、没有结尾斜杠。

**api 一直在重启，日志里有 `ENV=prod 时必须把 JWT_SECRET 设为至少 32 位的随机字符串`**

`.env` 里的 `JWT_SECRET` 还是默认值或太短，重新运行第 6 步的 `sed` 命令（先把它改回 `JWT_SECRET=CHANGE_ME`），然后 `docker compose up -d --force-recreate api worker migrate`。

**构建到一半失败，或者 worker 莫名重启**

多半是内存不够被系统杀了。用 `dmesg | grep -i oom` 确认，检查 `free -h` 里 swap 是否存在（第 4 步）。

**`docker compose` 报 `!reset` 相关的错误**

Docker Compose 版本太低，按第 2 步安装最新版。

**网页打开是 502**

api 还没启动好或者挂了：`docker compose ps` 看状态，`docker compose logs --tail 100 api` 看原因。
