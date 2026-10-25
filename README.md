# 飞牛 NAS 日志监控机器人

监控飞牛 NAS 事件日志（数据库 `logger_data.db3` 的 `log` 表），并推送多平台通知。

## 功能特性

- **数据库轮询**：定时轮询 eventlogger 的 SQLite 数据库，仅处理启动后的新记录
- **多平台通知**：企业微信、钉钉、飞书（均支持结构化消息）、通用 Webhook
- **Web 配置页**：内置配置 UI（默认端口 `18080`），支持 Webhook、事件类型、勿扰、标题前缀、推送记录等；保存后热加载，一般无需重启
- **Web 访问控制**：所有配置接口均需登录；首次设置密码需填写启动日志中的初始化码（或用 `FNMB_WEB_PASSWORD` 预置），`config.json` 内仅保存盐值与哈希；配置页中的 Webhook key、Token 会打码显示
- **schema 自检**：首次连接各飞牛数据库时打印实际列名，列名与代码不符时告警（否则查询会静默返回空数据，难以排查）
- **智能去重**：时间窗口去重（默认 300 秒）
- **磁盘事件合并**：同类型磁盘唤醒/休眠在时间窗口内合并推送
- **HTTP 连接池**：统一 HTTP 管理与重试
- **勿扰模式**：可设置时段内不推送，结束后汇总为一条消息
- **通知失败自愈**：连续失败达到阈值时可触发进程内重启通知链路（可通过环境变量关闭或调参）
- **Docker 部署**：支持 Docker Compose 一键部署，带健康检查
- **配置热加载**：通过 Web 保存 `config.json` 后自动生效；环境变量仍优先生效且不会被文件覆盖

### 支持的事件类型

以下为 `monitor_events` 中允许配置的 `eventId`（与数据库一致；旧配置里已移除的类型会在启动时被过滤）：

- **登录**：`LoginSucc`, `LoginSucc2FA1`, `LoginFail`, `Logout`
- **硬盘**：`FoundDisk`, `InsertDisk`, `EjectDisk`, `StorageBroken`, `STORAGE_DEGRADED`, `DiskWakeup`, `DiskSpindown`, `DISK_IO_ERR`
- **应用**：`APP_CRASH`, `APP_UPDATE_FAILED`, `APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION`, `APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE`, `APP_STARTED`, `APP_STOPPED`, `APP_UPDATED`, `APP_INSTALLED`, `APP_AUTO_STARTED`, `APP_UNINSTALLED`
- **系统**：`CPU_USAGE_ALARM`, `CPU_USAGE_RESTORED`, `CPU_TEMPERATURE_ALARM`, `MEMORY_USAGE_ALARM`, `MEMORY_USAGE_RESTORED`
- **UPS**：`UPS_ONBATT`, `UPS_ONBATT_LOWBATT`, `UPS_ONLINE`, `UPS_ENABLE`, `UPS_DISABLE`
- **SSH**：`SSH_INVALID_USER`, `SSH_AUTH_FAILED`, `SSH_LOGIN_SUCCESS`, `SSH_DISCONNECTED`（优先从宿主机 `journalctl -u ssh/sshd` 采集；需按宿主机实际存在的目录挂载 `/var/log/journal` 或 `/run/log/journal`，并挂载 `/etc/machine-id`）
- **文件与共享**：`ARCHIVING_SUCCESS`, `DeleteFile`, `MovetoTrashbin`, `SHARE_EVENTID_DEL`, `SHARE_EVENTID_PUT`
- **服务开关**：`WEBDAV_ENABLED` / `WEBDAV_DISABLED`, `SAMBA_ENABLED` / `SAMBA_DISABLED`, `DLNA_ENABLED` / `DLNA_DISABLED`, `FTP_ENABLED` / `FTP_DISABLED`, `NFS_ENABLED` / `NFS_DISABLED`
- **防火墙与安全**：`FW_ENABLE`, `FW_DISABLE`, `SECURITY_PORTCHANGED`
- **虚拟机**：`SHUTDOWN_VM`, `STATUS_RUNNING_VM`, `DESTROY_VM` 及其他名称含 `VM` 的动态事件（logger 中出现即允许配置）
- **备份任务**：`BACKUP_TASK_SUCCESS`, `BACKUP_TASK_FAILED`, `BACKUP_TASK_PARTIAL_SUCCESS`（读 `basic_backup.db3`）
- **影视库**：`TRIM_RESOURCE_ADDED`, `TRIM_SCRAPE_SUCCESS`, `TRIM_RESOURCE_DELETED`, `MEDIA_LOGIN_SUCC`, `MEDIA_LOGOUT`, `MEDIA_USER_CREATED`（读 `trimmedia.db` / `trimactivity.db`；删除事件来自 `media_delete` 表，表不存在时该类事件静默不推送）
- **相册**：`PHOTO_SHARE_CREATED`, `PHOTO_SHARE_EXPIRED`, `PHOTO_DEVICE_REGISTERED`, `FACE_RECOGNITION_UPDATED`（读 `photo.db`）
- **任务计划**：`SCHEDULER_TASK_SUCCESS`, `SCHEDULER_TASK_FAILED`, `SCHEDULER_TASK_CONDITION_FAILED`（读 `scheduler.db`）
- **Docker 容器**：`DOCKER_CONTAINER_CREATE/START/STOP/DIE/OOM/KILL/PAUSE/UNPAUSE/RESTART/DESTROY`（需挂载 `docker.sock`，等同授予 Docker API 访问能力）
- **外网 IP**：`WAN_IP_CHANGED`
- **定时巡检**：`NAS_PATROL_REPORT`（Cron 调度，独立于日志事件勾选）

> `NAS_PATROL_REPORT` 为内部事件类型，不在 `monitor_events` 白名单内，通过「巡检任务」开关启用。

## 日志存储

- **触发推送的原始数据**写入 `./data/logs`（可配置 `log_dir`），按事件类型与日期分文件；保留天数由 `log_retention_days` 控制。
- **运行日志**：`./data/logs/monitor_YYYYMMDD.log`；应用运行日志保留天数由 `max_log_age` 控制。
- **游标**：`./data/cursor/db_poller_cursor.txt`，记录已处理到的 `log` 表 id。

### 管理工具

```bash
# 需在项目根目录，或设置 PYTHONPATH
python tools/log_manager.py stats
python tools/log_manager.py recent --hours 24
python tools/log_manager.py type LoginSucc --limit 10
python tools/log_manager.py cleanup 30
```

## 使用方法

### 1. [配置通知渠道](docs/notification-channels.md)

至少配置一个推送渠道；环境变量与 Web 配置页二选一即可（亦可混用）。各平台图文说明见 **[配置通知渠道](docs/notification-channels.md)**（内含企业微信、钉钉、飞书、通用 Webhook 的独立文档入口）。

未配置任何 Webhook 时进程仍可启动，仅提供 Web 配置页；配置完成后自动开始监控与推送。

### 2. 主要配置项（`config/config.json`）

| 项 | 说明 |
| --- | --- |
| `wechat_webhook_url` / `dingtalk_webhook_url` / `feishu_webhook_url` | 各平台 Webhook URL |
| `webhook_params` | 通用 Webhook JSON（可多个，`\|` 分隔），见 [通用 Webhook](docs/channels/webhook.md) |
| `title_prefix` | 推送标题前缀，留空则使用默认「飞牛NAS」 |
| `monitor_events` | 要监控的事件 ID 列表 |
| `log_level` | 日志级别 |
| `log_dir` / `cursor_dir` | 日志与游标目录 |
| `logger_db_path` | SQLite 数据库路径 |
| `logger_poll_interval` | 轮询间隔（秒） |
| `resume_from_cursor_on_start` | 启动时是否沿用已保存游标（默认关闭）。关闭=只推送启动后的新记录；开启=补推停机期间写入的日志，适合容器短期重启不想漏事件 |
| `http_pool_size` / `http_retry_count` / `http_timeout` | HTTP 客户端参数 |
| `dedup_window` | 去重时间窗口（秒） |
| `log_retention_days` | 原始推送日志保留天数 |
| `max_log_age` | 应用运行日志 `monitor_*.log` 保留天数 |
| `dnd_enabled` / `dnd_start_time` / `dnd_end_time` | 勿扰开关与时段（HH:MM，可跨日） |
| `push_markdown_enabled` | 结构化消息（默认开启）：企业微信/钉钉用 markdown、飞书用富文本 post 按行分条展示；通用 Webhook 由其 body 模板决定。渠道不支持时自动回退纯文本 |
| `media_batch_summary_enabled` | 高频同类事件聚合推送（默认开启）：影视入库/刮削/删除、文件删除与回收站、共享变更、Docker 容器生命周期（创建/启动/停止/重启/暂停/销毁）等易突发的高频同类事件在静默窗口内合并为一条结构化消息（标题带条数、正文逐条列出）；影视登录/退出、Docker 异常（OOM/DIE/KILL）等会话与告警事件不参与聚合，保持逐条及时推送 |
| `media_batch_window` | 聚合静默窗口（秒，默认 90）：窗口内无新事件才发送；单条滞留超过窗口 4 倍强制发出；`0` 表示按轮询轮次立即发送 |
| `push_emoji_enabled` | 已废弃：推送文案模板已不再携带 emoji 图标，此开关保留仅为兼容旧配置，不再产生效果 |
| `ssh_ignore_loopback` | 忽略来源为 127.0.0.1 / ::1 的 SSH 登录成功与断开（默认关闭；失败告警不受影响；SSH 经 frp 等内网穿透接入时勿开启）。环境变量 `SSH_IGNORE_LOOPBACK` |

#### 各渠道消息形态

下表按各平台官方文档核对（2026-10 核实）：

| 渠道 | 消息形态 | 各平台限制与说明 |
| --- | --- | --- |
| 企业微信（群机器人） | `markdown`（`push_markdown_enabled` 关闭时为 `text`） | `content` **最长 4096 字节**、必须 UTF-8；群机器人 markdown 不支持列表语法（`- ` 会原样显示），明细直接按行渲染并支持 `##` 标题；超长按字节截断并标注 |
| 钉钉（自定义机器人） | `markdown` | `text` **最大 5000 字符**；`title` 另有独立上限（首屏会话透出内容），故预留标题长度后按字符截断；关键词/加签安全设置仍按原配置生效，关键词需出现在消息文本中 |
| 飞书（自定义机器人） | `post` 富文本 | `content.post.zh_cn.{title, content:[[{tag:"text", text}]]}`；**请求体不超过 20KB**，超长按字节截断；不支持时回退 `text` |
| 通用 Webhook | 由 `body` 模板决定 | 支持模板变量，形态由使用者掌控 |

渠道不支持结构化消息时会**自动回退纯文本**，不会丢通知。若某渠道明确拒绝富文本（格式类错误），进程内会记住该渠道，后续直接发纯文本，避免每条通知都先失败一次污染推送记录 —— 重启进程即重新探测，平台升级后无需改配置。

建议保存配置后用配置页的「测试推送」确认目标群里的实际渲染效果。

官方文档：[企业微信 消息推送配置说明](https://developer.work.weixin.qq.com/document/path/91770) ｜ [钉钉 自定义机器人发送消息的消息类型](https://open.dingtalk.com/document/orgapp/custom-bot-send-message-type) ｜ [钉钉 消息通知类型](https://open.dingtalk.com/document/development/message-types-and-data-format) ｜ [飞书 自定义机器人使用指南](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)

#### 联调：数据库 schema 自检

飞牛的数据库（`logger_data.db3`、`trimmedia.db`、`basic_backup.db3`、`scheduler.db` 等）没有公开文档，跨版本可能改表结构。**列名与真机不符时不会报错，而是查不到数据、静默不推送**，很难排查。

因此程序首次成功连接每个库时会自动做一次自检，日志里会打出：

- `INFO` — 每张表**实际探测到的列名**（与代码依赖的列对照即可发现不匹配）
- `WARNING` — 「代码依赖但表里不存在」的表或列，直接指出坏点

排查不到推送时，先看启动日志里的 `[schema 自检]` 行。若发现飞牛改了列名，欢迎提 issue 附上该行输出。

首次在 Web 中设置密码后，会在同文件写入 `web_password_salt`、`web_password_hash`（请勿手工泄露）。旧版的 `web_password_enabled` 已废弃，不再能关闭鉴权。

**首次设置密码**：为防止局域网内他人抢先设密，首次设置需填写初始化码。初始化码在启动时打印到日志（`docker logs 容器名`），同时写入配置目录的 `.setup_code`（权限 600），设置密码后自动失效。也可用环境变量 `FNMB_WEB_PASSWORD` 预置初始密码（至少 8 位）。

### 3. 常用环境变量

除上述 Webhook 外，还可通过环境变量覆盖（**已设置的环境变量不会被 `config.json` 覆盖**）：

- `LOGGER_DB_PATH`、`LOGGER_POLL_INTERVAL`
- `RESUME_FROM_CURSOR_ON_START`：启动时沿用已保存游标（默认关闭）
- `MONITOR_EVENTS`（逗号分隔）
- `LOG_LEVEL`、`HTTP_POOL_SIZE`、`HTTP_RETRY_COUNT`、`HTTP_TIMEOUT`、`DEDUP_WINDOW`
- `LOG_RETENTION_DAYS`、`MAX_LOG_AGE`
- `PUSH_MARKDOWN_ENABLED`：结构化消息开关（`1`/`true` 开启）
- `MEDIA_BATCH_SUMMARY_ENABLED`：高频同类事件聚合推送（默认开启）
- `MEDIA_BATCH_WINDOW`：聚合静默窗口（秒，默认 90）
- `MINIMAL_PUSH_ENABLED`：极简推送（一行文案）
- `DISPLAY_TIMEZONE`：通知与记录的时间显示时区（默认 `Asia/Shanghai`；各数据源统一按此时区展示）
- `UI_PORT`：Web 端口（默认 `18080`）；`UI_HOST`：监听地址（默认 `0.0.0.0`，仅本机访问可设 `127.0.0.1`）
- `FNMB_WEB_PASSWORD`：未设密码时作为初始 Web 密码（至少 8 位）
- `FNMB_DISABLE_AUTH=1`：关闭应用内鉴权，仅在前面已有自建反代鉴权时使用，**切勿直接暴露到局域网**。为避免误开启，该变量**必须配合** `FNMB_DISABLE_AUTH_ACK=1` 才生效；只设前者时鉴权保持开启并在日志中提示
- `LOGTIME_DISPLAY_OFFSET_SECONDS`：修正日志时间显示（默认 `28800`，即 +8 小时；若显示不准可调整）
- `NOTIFY_RESTART_ENABLED`、`NOTIFY_RESTART_CONSECUTIVE`、`NOTIFY_RESTART_WINDOW`、`NOTIFY_RESTART_COOLDOWN`：通知链路失败重启策略（默认关闭；推送限流/额度用尽时重启无法恢复，反而会重复推送）
- `APP_HOME`：自定义应用根目录（影响 `config.json` 解析路径，一般 Docker 内为 `/app`）

### 4. 数据库路径

- 默认：`/usr/trim/var/eventlogger_service/logger_data.db3`
- 覆盖：`LOGGER_DB_PATH=/path/to/logger_data.db3` 或在 `config.json` 中设置 `logger_db_path`
- Docker 需将该文件或所在目录以只读等方式挂载进容器（见下方 Compose 示例或仓库内 `docker-compose.yml`）。

### 5. 启动

#### 方案一：GitHub 构建镜像，NAS 只负责拉取（推荐）

`.github/workflows/build-image.yml` 会在 GitHub 服务器上构建镜像并推送到 GitHub Container Registry，飞牛只拉取运行。好处是**绕开国内直连 Docker Hub 拉 `python:3.11-slim` 超时**的问题 —— GitHub 运行器在海外，不需要任何镜像源配置。

1. 把代码推到 GitHub（fork 或自己的仓库均可）。
2. 仓库 **Actions** 页 → 左侧 `Build and Push Image` → **Run workflow**（也可直接 push 到 `main` 自动触发）。默认只构建 `linux/amd64`（飞牛 NAS 用这个）；需要 ARM 机器就在 `platforms` 里填 `linux/amd64,linux/arm64`，构建时间会明显变长。
3. **把镜像设为公开**：构建完成后到 GitHub → 头像 → **Packages** → 选中该镜像 → **Package settings** → **Change visibility** → **Public**。GHCR 的包默认是私有的，不改公开的话飞牛拉取会 401。
4. 在飞牛上新建 `docker-compose.yml`：

```yaml
services:
  fn-message-bot:
    image: ghcr.io/<你的用户名>/<仓库名>:latest   # 全小写，例如 ghcr.io/young-zj66/fnmessagebots:latest
    container_name: fn-message-bot
    restart: unless-stopped
    ports:
      - 18080:18080
    volumes:
      - ./data/logs:/app/data/logs:rw
      - ./data/cursor:/app/data/cursor:rw
      - ./config:/app/config:rw
      - /usr/trim/var/eventlogger_service:/usr/trim/var/eventlogger_service:ro
      - /var/log/journal:/var/log/journal:ro
      - /run/log/journal:/run/log/journal:ro
      - /etc/machine-id:/etc/machine-id:ro
    environment:
      - TZ=Asia/Shanghai
```

> 镜像名必须**全小写**：仓库名若含大写（如 `FNMessageBots`），workflow 会自动转成小写。

5. 拉取并启动：`docker compose pull && docker compose up -d`。以后升级只需再 `docker compose pull && docker compose up -d`（配置与游标在宿主目录，不会丢）。

**不想把仓库设为公开？** 两条替代路径：

- 在 NAS 上登录私有 registry：`docker login ghcr.io -u <用户名> -p <PAT>`（PAT 需 `read:packages` 权限），再 normal pull。
- 让 workflow 额外推一份到 Docker Hub：在仓库 **Settings → Secrets and variables → Actions** 添加 `DOCKERHUB_USERNAME` 与 `DOCKERHUB_TOKEN`（用 Access Token，不是登录密码），workflow 会自动把已构建好的镜像**复制**一份到 Docker Hub（`docker buildx imagetools create`，不重新构建）。

#### 方案二：在 NAS 上直接构建

```bash
docker compose up -d --build
```

NAS 拉 `python:3.11-slim` 可能超时，此时用国内镜像源：

```bash
docker compose build --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.11-slim
docker compose up -d
```

#### 方案三：本机构建后导出镜像

无 registry 时的兜底：

```bash
# 本机（需 Docker）
docker build --platform linux/amd64 -t fnmb:local .
docker save fnmb:local | gzip > fnmb.tar.gz

# 传到 NAS 后
gunzip -c fnmb.tar.gz | docker load
```

然后把 compose 里的 `build: .` 换成 `image: fnmb:local`。

#### 首次访问

浏览器打开 `http://<本机IP>:18080`，首次需输入**一次性初始化码**：看启动日志（`docker compose logs | grep -i 初始化`）或配置目录下的文件（`cat config/.setup_code`，权限 600，设置密码后失效）。也可用环境变量 `FNMB_WEB_PASSWORD` 预置密码（至少 8 位）。

#### 本地运行（无 Docker）

**依赖**：Python 3.9+（见 `pyproject.toml`）。先安装依赖：`pip install -e .` 或 `pip install -r requirements.txt`（与镜像一致）。

```bash
PYTHONPATH=. LOGGER_DB_PATH=./logger_data.db3 WECHAT_WEBHOOK_URL=xxx python3 src/main.py
```

## 项目结构

```
├── src/
│   ├── monitor/          # 数据库轮询、事件处理、schema 自检、巡检、外网 IP 监控
│   ├── notifier/         # 多平台通知（消息格式/文案表/内容构建/渠道发送分层）、连接池
│   ├── utils/            # 日志、存储、去重缓存、健康检查、推送统计与记录
│   ├── web/              # Web 配置 UI（Flask；模板与 assets/ 下外置 CSS/JS）
│   ├── config.py
│   ├── version.py        # 版本号单一来源
│   └── main.py
├── assets/               # 前端静态资源（css/js/fonts/icons）
├── config/config.json    # 配置文件（可挂载，Web 可写）
├── data/logs             # 运行日志与推送存储（运行时生成）
├── data/cursor           # 轮询游标、推送记录库（运行时生成）
├── tests/                # 回归测试（纯标准库，不联网、不需要第三方依赖）
├── tools/                # 运维脚本（随镜像分发，docker exec 可用）
│   ├── log_manager.py                    # 推送日志查看/清理
│   └── dedupe_disk_merge_records.py      # 磁盘合并推送历史重复记录清理
├── docs/                 # 文档（notification-channels 与各推送渠道子页）
├── .github/workflows/
│   └── build-image.yml   # 在 GitHub 上构建并推送镜像（GHCR）
├── Dockerfile
├── docker-compose.yml
├── deploy.sh             # 本地 compose 构建并启动
├── healthcheck.sh        # 容器健康检查
├── requirements.txt
├── pyproject.toml
└── LICENSE               # MIT 许可证全文
```

运行测试：`python tests/run_all.py`（详见 [tests/README.md](tests/README.md)）。

## FPK 原生应用打包

`fpk/` 是飞牛 FPK 安装包脚手架（Docker 型：安装即创建容器，镜像取自本仓库 GHCR），构建在独立临时目录完成：

```bash
python scripts/build-fpk.py                                  # 产物 dist/FnMessageBot-<版本>.fpk
python scripts/verify-fpk.py dist/FnMessageBot-<版本>.fpk    # 校验包内容
```

- 服务端口固定 18230（manifest 的 `service_port` 与桌面入口共同约定）；安装向导只采集访问密码（PBKDF2 落盘，明文不保存）。
- native 型脚手架已归档在仓库外（`fnmb-archive/fpk-native`），可用 `python scripts/build-fpk.py --scaffold <目录>` 出 native 包（产物带 `-native` 后缀）。

## 从 2.6.x 升级到 2.7+

2.7.0 起推送渠道收敛为**企业微信、钉钉、飞书、通用 Webhook** 四个，升级前请确认：

1. **仍在用 Bark / PushPlus / SMTP 邮件 / MeoW 的**：这些渠道已移除，需改用「通用 Webhook」对接（各平台均为一个 HTTP POST，`webhook_params` 的 body 模板可自定义请求体；配置方法见 [通用 Webhook](docs/channels/webhook.md)）。
2. **旧 `config.json` 无需手工清理**：其中 `bark_url`、`pushplus_params`、`smtp_params`、`meow_params` 等残留键会被忽略，并在启动日志中提示「可安全删除」。
3. **环境变量**：`BARK_URL`、`SMTP_PARAMS`、`MEOW_PARAMS` 等不再生效，启动日志会提示。
4. Web 配置页中已保存的旧渠道行会在下次保存时自动丢弃；升级后请到「推送渠道」页确认渠道列表符合预期。

## 故障排除

- **巡检的温度 / 健康为 `--`（SMART 授权）**：native 部署需要给应用账号开 SMART 访问（飞牛托管的普通账号权限有限，只加 disk 组不能保证生效）。在 NAS SSH 用管理员账号执行（容器部署改为在 compose 映射 `/dev/sdX` 后重建）：

  ```bash
  sudo apt-get install -y smartmontools libcap2-bin acl
  sudo setcap cap_sys_rawio,cap_sys_admin+ep "$(command -v smartctl)"   # SATA 用 rawio，NVMe 需 admin
  sudo setfacl -m u:FnMessageBot:r -- /dev/sda /dev/sdb /dev/sdc /dev/sdd   # 按实际盘名替换
  sudo -u FnMessageBot smartctl -A -d sat /dev/sda | grep -i temper      # 验证；部分控制器需 -d sat
  ```

  授权后通常无需重启应用，保存配置并「立即巡检一次」验证。设备节点重启后重建，ACL 会丢失，需重跑 `setfacl`（升级应用时会自动重授）。部分 SAT 转换层设备自动探测失败时程序会自动以 `-d sat` 重试。工具升级后 capability 可能需重授；不要给 Python 解释器加 capability。
- **收不到通知**：先看启动日志里的 `[schema 自检]` 行（见上文），再检查 Webhook、网络与 `docker compose logs`。
- **收到的不是结构化消息（纯文本）**：确认 `push_markdown_enabled` 已开启且镜像为最新（结构化推送在 2.6.0 引入）；日志中出现「结构化消息发送失败，回退纯文本」说明渠道拒绝了 markdown（多为安全设置或旧版企业微信客户端），进程会记住该渠道改发纯文本，重启容器可重新探测。
- **影视入库一条一条刷屏**：确认「影视/相册事件聚合推送」已开启（默认开启）并按需调整 `media_batch_window` 静默窗口。
- **时间不对**：调整 `LOGTIME_DISPLAY_OFFSET_SECONDS`（默认已为 +8 小时）。
- **重复通知**：调大 `dedup_window`（秒）。
- **Web 配置页打不开**：确认端口映射与防火墙；容器内可设 `UI_PORT` 并与 `ports` 一致。监听失败或 Web 服务线程退出后，监督线程每 5 秒自动恢复并写 monitor_*.log（每分钟至多一条）；容器健康检查（/healthz）验证进程与配置 UI 均正常。反代部署下限流按直连 IP 统计，可在 compose 设 `FNMB_TRUSTED_PROXIES`（逗号分隔可信网段）按真实来源限流。
- **忘记 Web 密码**：在可信环境下编辑 `config.json`，删除 `web_password_salt` 与 `web_password_hash` 后重启，再用日志或 `config/.setup_code` 中的初始化码重新设置密码。
- **改了代码但行为没变**：确认 NAS 拉到的是新镜像（`docker compose pull` 后再 `up -d`；本地构建需 `--build`）。用 `docker inspect <容器> --format '{{.Image}}'` 对比镜像 ID。

## 免责声明

- **非官方项目**：本仓库为独立开源作品，与飞牛 NAS、企业微信、钉钉、飞书等主体**无隶属、赞助或背书关系**；各产品名称与商标归权利人所有。
- **按现状提供**：除开源许可证另有约定外，作者不对本软件的**正确性、完整性、安全性或持续可用性**作任何明示或默示担保；详见 [`LICENSE`](LICENSE) 中的免责声明条款。
- **使用风险自负**：部署、配置 Webhook / 第三方推送、开放 Web 配置端口、挂载系统路径等行为均由使用者自行评估与承担；请妥善保管 `config.json`、访问密码与各类 Token，**勿将密钥提交到公开仓库**。
- **数据与业务影响**：监控与推送依赖 NAS 日志库、网络与各通知渠道服务；**不保证**通知必达、实时或无遗漏，亦不对因依赖本软件而产生的业务中断、数据误解或决策损失承担责任。
- **合规**：使用本软件及对接的第三方服务时，请遵守相应平台规则、隐私政策及所在地法律法规。

## 许可证

本项目基于 [Sunanang/FNMessageBots](https://github.com/Sunanang/FNMessageBots) fork 而来，本仓库的修改同样以 [MIT License](https://opensource.org/licenses/MIT) 发布，许可证全文见仓库根目录 [`LICENSE`](LICENSE)。按 MIT 要求，`LICENSE` 中的原始版权声明与许可声明不得移除。
