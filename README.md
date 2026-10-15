# Telegram 群组 PikPak 分享链接采集

[![build](https://github.com/Kongbai333/pikpakguangya/actions/workflows/docker.yml/badge.svg)](https://github.com/Kongbai333/pikpakguangya/actions/workflows/docker.yml)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![image](https://img.shields.io/badge/ghcr.io-kongbai333%2Fpikpakguangya-blue)](https://github.com/Kongbai333/pikpakguangya/pkgs/container/pikpakguangya)

用 Telegram 用户账号监听群组，抽取 `mypikpak.com/s/xxx`、提取码和备注，去重入库，
定期校验链接并抓取文件名，最后通过网页搜索和浏览。

## 技术栈

- 后端：Python + FastAPI + Telethon + SQLite
- 前端：Vue 3 + Naive UI + ECharts + Tailwind + Vite
- 代理：只使用可直接连接的标准代理 `http` / `https` / `socks5`

项目不再使用订阅、节点、sing-box 或 Clash 内核。

## 快速开始

### Docker（推荐）

```bash
# 直接拉镜像
docker run -d --name pikpak-collector \
  -p 8000:8000 \
  -v "$PWD/data:/data" \
  -e TZ=Asia/Shanghai \
  ghcr.io/kongbai333/pikpakguangya:latest
```

或者用 compose（仓库里带了 `docker-compose.yml`）：

```bash
docker compose up -d
```

第一次启动会在 `data/` 下生成 `config.toml`，填好这两项再重启容器：

- `[telegram]` 的 `api_id` / `api_hash`
- `[admin]` 的 `password` —— **留空的话管理后台一律拒绝登录**

管理页：`http://<主机>:8000/#/admin`

数据全在挂载出来的 `data/` 目录里（配置、SQLite 库、Telegram 登录态、日志、缩略图），
删容器重建不会丢。镜像同时支持 `linux/amd64` 和 `linux/arm64`。

### 从源码跑（Windows）

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 前台启动
powershell -ExecutionPolicy Bypass -File .\start.ps1

# 或后台常驻
powershell -ExecutionPolicy Bypass -File .\start.ps1 -Background
```

管理页：`http://127.0.0.1:8000/#/admin`

首次使用顺序：

1. 「设置」填写 Telegram `api_id` / `api_hash`。
2. 需要代理的话，去「设置 → 代理池」添加（可以直接粘 `tg://proxy` 链接），
   并在上面的「系统代理」里选一条给校验和秒传用。能直连就跳过这步。
3. 「账号监控」添加账号、绑定代理、勾选要监控的群。
4. 在账号上打开「采集」开关。之后程序启动时会自动开始采集，不用再手动点启动。

## 从源码构建镜像

```bash
docker build -t pikpak-collector .
```

多阶段构建：第一段用 Node 打包前端，第二段是 Python 运行时。前端那一段跑在
`$BUILDPLATFORM` 上，所以 `--platform linux/arm64` 交叉构建时不会被 QEMU 拖慢。

## 采集模型

- 采集开关按账号保存，每个账号一套独立的 session、心跳、群勾选和回溯游标，多个账号可以并行采集。
- 程序启动时自动拉起所有打开开关的账号；单个账号启动失败只记录状态和日志，不影响其他账号和后台服务。
- 第一次监控某个群时不读历史，从当前最新消息开始；之后每次启动会补齐上次采集位置到现在的消息，
  程序离线期间的消息不会漏。全程不调用已读接口，不会清掉 Telegram 的未读标记。
- 账号不绑代理就是直连，不会被拦住 —— 能直连 Telegram 的机器（海外 VPS、部分家宽）
  不需要代理；国内不挂代理连不上时，报错里会写清楚是连接问题。
- 新链接的即时校验队列全局共享，请求密度不随账号数量放大，避免触发 PikPak 风控。

## 代理模型

代理分两件事，各管各的：

### 系统代理

一条，全局出口。**PikPak 链接校验、分享详情接口、秒传、Telegram Bot API** 都走它 ——
这些请求不属于任何 TG 账号，需要一个统一的出口。在「设置 → 代理池」顶部选一条，
不选就是直连。

### 代理池

一堆，**绑给具体的 TG 账号**。每个号可以走不同的出口，采集和登录都用自己那条。
「添加账号」时选，之后在「编辑账号」里改。

**账号不绑代理就是直连** —— 这是合法配置，不是没配好。部署在能直连 Telegram 的机器上
（海外 VPS、部分家宽）完全可以全部直连。

### 添加代理

可以直接粘一条链接让它自动解析，也可以手填：

| 粘贴的写法 | 识别为 |
|---|---|
| `tg://proxy?server=…&port=…&secret=…` | MTProto 代理 |
| `tg://socks?server=…&port=…&user=…&pass=…` | SOCKS5 |
| `https://t.me/proxy?…` / `https://t.me/socks?…` | 同上（网页版分享出来的） |
| `socks5://user:pass@1.2.3.4:1080` | 标准 URL |
| `1.2.3.4:1080` | 地址 + 端口 |
| `1.2.3.4:1080:用户名:密码` | 冒号分隔（代理商常这么发） |

### 类型的区别

| 类型 | 给 Telegram 用 | 当系统代理 |
|---|---|---|
| `http` / `https` / `socks5` | ✅ | ✅ |
| `mtproto`（`tg://proxy`） | ✅ | ❌ |

MTProto 代理说的是 MTProto 协议，代理不了 PikPak 的 HTTPS 请求，所以只能绑给 TG 账号，
系统代理的下拉里会把它禁掉。

另外，`secret` 以 `ee` 开头的是 fake-TLS（域名伪装）代理，**Telethon 不实现域名伪装**
（它会把 secret 后面的域名直接丢掉），这类代理大概率连不上；`dd` 开头或裸 secret 是正常的。
添加时如果 secret 是 `ee` 开头，页面会直接提示。

## 页面

| 页面 | 功能 |
|---|---|
| 仪表盘 | 采集监控（雷达 + 实时活动条）、指标卡、每日走势、状态构成、来源构成 |
| 链接 | 工具栏筛选、表头排序、批量操作条、校验、隐藏、删除、复制、直接秒传 |
| 媒体 | Telegram 缩略图瀑布流，点开看大图；原媒体跳转 Telegram，不缓存原文件 |
| 秒传 | 粘贴链接批量建任务；可勾选要传的文件（支持按大小批量筛选）、暂停 / 终止 / 重试 |
| 设置 | 应用凭据、Bot、PikPak、光鸭、秒传参数、代理池（系统代理 + 账号出口）、前台开关、环境信息、日志 |
| 账号监控 | 左边账号列表，右边全部群组和监控开关；账号可编辑备注、手机号和绑定代理 |
| 日志 | 采集日志和服务进程日志，自动刷新 |
| 前台浏览 | 给访客看的只读卡片流，点一下复制链接 / 提取码。**不显示来源** |

界面按 Ctrl / ⌘ + K 打开命令面板，可以搜页面名或执行动作。

## 前台浏览页开关

根地址 `/` 是给访客看的只读浏览页，可以在「设置 → 前台浏览页」一键关掉。
关掉之后：

- `/` 返回一个「浏览页已关闭」的提示页（HTTP 404），不再吐前端入口
- `/api/items`、`/api/stats` 一律 403 —— **只藏页面不拦接口等于没关**，数据照样能直接拉走
- `data.js` / `shuju.json` 这类导出的静态快照也一并停掉

管理后台 `/#/admin` 不受影响，照常登录使用。想临时分享给别人看就打开，不想就关掉。

前台**不返回来源**（`source` 字段直接不吐给 `/api/items`）：它是采集到这条链接的群名，
属于库主的隐私。管理页走 `/api/admin/links`，那边照旧有来源列和来源筛选。

## 选择性秒传

任务解析完成后，明细列表里每一条都能勾选 —— 勾掉的文件不会被传，留在
`pending` 状态，随时可以改主意。批量操作（全选本页 / 全部选中 / 按大小筛选）
由后端直接执行，不受「详情只显示前 300 条」的限制。

任务收尾时，没勾选的待处理条目会被标成「跳过」，否则任务已经结束但计数对不上
（显示 80/100，剩 20 条永远停在 pending）。

### 秒传的链接也会进链接库

不管是从网页「秒传」页手动加的，还是直接给 Bot 发链接建的，**分享链接都会登记进
链接库**（来源标成「手动秒传」/「Bot 秒传」），和采集到的链接一样参与搜索、
校验和失效清理。

判重靠 `shares.link_id` 的唯一约束：已经采集过的链接不会多出一行，
来源也保留最早那个（先在某群里采到、后来又秒传了一次，来源仍然是那个群名）。
只粘一个分享 ID 也认，入库时会补成完整的 `https://mypikpak.com/s/<id>`。

## 时区

数据库里存的时间一律是 UTC；`[app] timezone` 只影响「按天 / 按分钟」分桶的统计
和页面标签。留空 = 跟随本机时区。

不配的话，UTC+8 的用户在早上 8 点前看到的「今天」其实是昨天 —— 仪表盘的
每日走势和实时活动条都会差一天。Windows 上填 IANA 名字需要装 `tzdata`
（已在 requirements.txt 里），装不上会自动退回本机时区并写一条告警。

## Telegram Bot

在「设置」里填 Bot Token 和你的 TG 用户 ID，Bot 只接收这个 ID 的私聊。

**直接发消息就能用**：

- 发 PikPak 分享链接 → 自动入库并加入秒传队列
- 提取码可以另发一条（4 位码）→ 自动补到上一条链接
- 一条消息里放多个链接 → 一起处理

**命令**：

| 命令 | 说明 |
|---|---|
| `/tasks` | 秒传任务列表（只读，操作请到网页的「秒传」页） |
| `/search 关键词` | 搜索链接；结果带「秒传 / 复制」按钮，可直接建任务 |
| `/links` | 链接库统计；点分类按钮直接列出该状态的链接 |
| `/accounts` | 账号列表 → 账号菜单：打开 / 关闭采集、添加群组、群组采集开关 |
| `/system` | Bot / 光鸭 / PikPak / 采集 / 账号的运行状态 |
| `/cleanup` | 清理失效链接（带二次确认） |
| `/help` | 用法说明 |
| `/cancel` | 退出当前操作 |

任务进度和完成通知会自动推送；同一个任务的进度消息是编辑同一条，不会刷屏。

账号、代理池和链接数据都在 `pikpak.db`。`config.toml` 只保存 Telegram 凭据和存储配置。

媒体缩略图保存在 `media/thumbs/`，数据库保存媒体元数据。媒体采集默认关闭，
需要在「账号监控」里对具体群组单独打开“媒体”开关。

## 主要接口

```text
GET    /api/proxies                  代理池列表
POST   /api/proxies                  添加代理（带 raw 时按粘贴的链接解析）
POST   /api/proxies/parse            解析粘贴的代理链接，给添加表单做预览
PUT    /api/proxies/{id}             编辑代理
DELETE /api/proxies/{id}             删除代理
POST   /api/proxies/{id}/test        测试代理连通性
GET    /api/proxies/system           系统代理
PUT    /api/proxies/system           设置系统代理（proxy_id=0 表示直连）

GET    /api/accounts                 账号列表
PUT    /api/accounts/{id}/profile    改备注名 / 手机号
PUT    /api/accounts/{id}/proxy      绑定 / 解绑代理
POST   /api/accounts/{id}/collect    打开 / 关闭该账号的采集

GET    /api/monitor                  账号及其监控群
GET    /api/activity                 最近 N 分钟的采集活动（仪表盘实时监控用）
GET    /api/logs                     采集 / 服务日志
GET    /api/listener                 所有账号的采集状态

GET    /api/web-public               前台浏览页开关状态
PUT    /api/web-public               打开 / 关闭前台浏览页
```

`/api/items` 和 `/api/stats` 是仅有的两个对访客开放的接口，受前台开关控制；
其余全部要求管理员登录。

## 常用命令

```powershell
.\build-web.ps1       # 构建前端到 web/dist
.\stop.ps1            # 停止后台服务
.\start.ps1           # 前台启动
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

默认不访问网络；需要真连 `mypikpak.com` 的用例要加 `--run-network`。
CI 会在每次 push / PR 时跑测试并构建前端，通过后才构建镜像。

## 安全

- `config.toml`、`session/`、`*.db` 和 `logs/` 都不要外传。
- Telegram 登录态文件等同于账号凭据。
- 代理密码保存在本机 SQLite 数据库中。

**要放到公网的话**：

- 必须在 `[admin] password` 里设一个强密码 —— 留空时后台接口全部返回 503，
  等于后台不可用，但公开接口仍然开放。
- 前面挂一层 HTTPS 反向代理。管理登录的 Cookie 带 `HttpOnly` + `SameSite=Lax`，
  走 HTTPS 时会自动加 `Secure`。
- 不想让访客看到链接库，就把「设置 → 前台浏览页」关掉，公开接口会一起拒绝。
- 登录失败 5 次会封该 IP 5 分钟，但这是进程内的内存计数，重启即清零，
  别把它当唯一防线。

## 开源协议

[Apache License 2.0](LICENSE)，第三方组件见 [NOTICE](NOTICE)。
