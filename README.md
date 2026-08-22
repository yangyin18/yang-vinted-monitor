# Vinted 多账号商品监控系统

一套给客户的**多账号 Vinted 商品监控产品**：后台服务抓取 Vinted 公开接口并持久化到 SQLite，Windows 桌面客户端把**全部账号的商品合并成一个列表**实时展示。

无需登录 cookie，匿名即可拿到商品的点赞数、售出状态、上架时间（照片时间戳近似）、价格变动；填登录 cookie 可额外解锁浏览量。核心难点是**在 Vinted 的 Cloudflare 风控下稳定抓取**——通过 TLS 指纹伪装 + IP 代理池轮换 + 失败自动切换解决。

```
[服务器]                        [本地 Windows]
┌────────────────────────┐      ┌─────────────────────────────┐
│  Scheduler 线程         │      │  vinted_client.py          │
│  定时全量抓取            │      │  customtkinter 深色 GUI     │
│   ↓                    │      │  - 全部账号商品合并单列表    │
│  curl_cffi 抓 Vinted   │      │  - 新上架蓝色高亮在前        │
│  (Chrome TLS 指纹)      │      │  - 已售置灰沉底             │
│   ↓                    │      │  - 赞数增量 +3/-2 高亮      │
│  SQLite 持久化          │      │  - 双击行浏览器打开         │
│  (items/snapshots)      │      │  - 批量加删账号/立即刷新    │
│   ↓                    │      └──────────┬──────────────────┘
│  REST API              │                 │ HTTP (无鉴权,内网)
│  /api/status /accounts │                 ▼
│  /api/items /refresh   │      ┌─────────────────────┐
└──────────────┬─────────┘      │  IP 池(三种可选)     │
               │                │  ① 青果短效住宅池     │
               ▼                │  ② 自建 3proxy IPv6池│
      Vinted Cloudflare        │  ③ 本地 Clash 切机场节点│
      (403 自动切下一个出口)     └─────────────────────┘
```

## 功能特性

| 能力 | 说明 |
|---|---|
| 多账号监控 | 账号清单文件 / API 批量添加，支持主页链接、数字ID、用户名三种写法，自动解析并缓存 user_id |
| 匿名即可用 | 抓 `api/v2/wardrobe/{user_id}/items` 公开接口，无登录 cookie；游客 JWT 跨 IP/跨轮次复用 |
| 状态检测 | 在售 / 已售 / 被预留 / 草稿 / 已隐藏，售出靠"商品从列表消失"检测，按账号隔离防误判 |
| 数据持久化 | SQLite 全量 upsert + 每次抓取记赞数/价格快照，赞增量（fav_delta）由快照推算，跨重启保留 |
| 赞增量高亮 | 服务端算 `fav_delta`，客户端 +3 绿色 / -2 红色 |
| 保留期清理 | 超过 `retention_days`（默认7天）没再出现的商品自动清理 |
| 一键删除已售 | `deleted_items` 表防回灌——删了的商品下轮抓取不会再回来 |
| 立即刷新 | 手动刷新可中断在跑的一轮，排队重抓，点击不丢失 |
| 手动/定时调度 | `manual` 模式只在 API 触发时抓（零流量空闲）；`auto` 模式按间隔 + 可选时段窗口（如 02:00–13:00） |
| 并发抓取 | `workers>1` 时 thread-local 锁定出口 IP 并行抓取，单账号失败不拖垮整轮 |
| 桌面客户端 | 全部账号商品合并单列表、本地状态筛选、上架时间正倒序、双击开链接；本机零数据落盘 |

## 技术栈

- **抓取层**：`curl_cffi`（伪装 Chrome 126 TLS 指纹，抗 Cloudflare 识别），`requests` 兜底
- **服务端**：FastAPI + uvicorn + SQLite（线程安全访问）
- **客户端**：customtkinter（深色主题）+ ttk.Treeview + requests
- **打包**：PyInstaller（解决 Anaconda 下 `tcl/ssl/ffi` DLL 收集问题）
- **部署**：bash 一键部署脚本 + systemd 服务单元，固定 `--workers 1` 防双调度线程

## 核心工程亮点

### 1. 抗 Cloudflare 的抓取工程
- **TLS 指纹伪装**：`curl_cffi` 以 `impersonate="chrome"` 发起请求，规避 TLS 指纹识别
- **游客 token 缓存复用**：访问首页种下游客 JWT 后缓存，实测**同一 token 换 12 个出口 9/12 成功**，每轮不再下载 ~1.9MB 首页，只带 cookie 请求商品接口，大幅降流量
- **逐出口轮换 + 403 自动切换**：每轮检查换一个出口 IP；`_get` 遇 403 / 网络错误自动切下一个重试（重试上限 30，防池子全灭时空转）
- **409 时自动刷新 token**：`wardrobe` 返回 401 → 强制重拉首页换新 token 后同出口重试
- **HEAD 探测省流量**：代理池存活探测用 `HEAD` 而非 `GET`，150 个代理一轮省 ~280MB

### 2. 页面改版不崩溃的鲁棒性
- 安全类型工具（`_dict_of` / `_to_int` / `_to_float`）：字段缺失、格式异常一律给安全默认值
- **"抓取失败"绝不当成"全部售出"**：接口返回非 JSON / `items` 非列表直接显式报错，而不是清空列表触发误判
- 售出检测按账号隔离：只对比该账号自己上次的记录，其他账号抓取失败不会污染判断
- 单条脏数据跳过：畸形条目不拖垮整轮

### 3. 多账号规模的代理池管理
- **青果短效住宅代理池**：每轮现提现用（短效 IP 存活 ~2.5 分钟），提取 → curl_cffi 并行探测对 vinted 回 200 的出口 → 写池文件 → 换入内存池。用 curl_cffi 而非 requests 探测（实测活率 124/150 vs 12/150）
- **主动防过期**：池子用满 100s 提前重提、连续 2 个账号 407 立即重提，永远赶在 IP 过期前
- **`_TOKEN_FAILED` 事件**：池子全被挑战时让其余账号快速失败，避免 30 个账号各空转一遍
- **线程安全轮换**：并行模式用 thread-local 锁定出口，全局游标 + 锁

### 4. 零成本自建 IPv6 代理池
- `setup_ipv6_pool/`：一台服务器给网卡加 1000 个 IPv6 地址，用 3proxy 起 1000 个 SOCKS5 端口，导出标准 `socks5h://user:pass@host:port` 格式
- 针对数据中心 IPv6 在 Cloudflare 前被随机 JS 挑战（403）的问题，做了**换出口直到首页 200** 才认为 token 拿到 + 抓取时 403 自动换出口的两层兜底

### 5. 客户端兼容旧版服务器
- 新 `/api/items` 一次拉全；旧版强制 `account` 参数 → 客户端自动降级为逐个账号拉取合并

## 目录结构

```
├── server.py            # FastAPI 服务端：调度 + SQLite 持久化 + REST API
├── monitor.py           # 核心抓取引擎（IP 池轮换 / token 管理 / 鲁棒性工具）
├── vinted_client.py     # 多账号 Windows 桌面客户端（customtkinter）
├── clash_pool.py        # 通过本地 Clash 切机场节点实现换 IP
├── server_config.json   # 服务配置模板（含代理池/调度/青果池配置）
├── accounts.txt         # 账号清单模板（每行一个账号）
├── proxy_pool.example.txt / clash_pool.example.txt   # 代理池模板
├── requirements*.txt    # 依赖
├── *.spec               # PyInstaller 打包配置
├── deploy/              # 一键部署脚本 + systemd 单元
├── setup_ipv6_pool/     # 自建 3proxy IPv6 SOCKS5 池脚本
└── assets/              # 应用图标
```

## 快速开始

```bash
pip install -r requirements.txt
```

**带持久化的单账号监控**：

```bash
python monitor.py --once                          # 跑一次看结果
python monitor.py                                 # 每 10 分钟检查一次
python monitor.py --proxy-pool proxy_pool.txt     # 用代理池轮换出口 IP
```

**多账号 GUI 客户端**（连服务器，全部账号商品合并一个列表）：

```bash
pip install -r requirements-client.txt
python vinted_client.py                           # 输入服务器地址连接
```

代理策略：默认自动探测常见本地代理端口（Clash 7890 / Clash Verge 7897 / V2RayN 10809 / SS 1080），海外网络自动直连；`--proxy none` 强制直连。

## 部署（服务器端）

```bash
scp monitor.py clash_pool.py server.py server_config.json accounts.txt \
    proxy_pool.txt requirements-server.txt root@<服务器IP>:/tmp/vinted-monitor/
scp -r deploy root@<服务器IP>:/tmp/vinted-monitor/
ssh root@<服务器IP> 'bash /tmp/vinted-monitor/deploy/deploy.sh'
```

脚本自动：装 Python 依赖 → 建 systemd 服务并启动 → 放行 22/8000 端口。健康检查：

```bash
curl http://127.0.0.1:8000/healthz        # {"ok":true}
curl http://127.0.0.1:8000/api/items      # 全部账号全部商品
```

**REST API**：`/healthz` `/api/status` `/api/accounts`(GET/POST/DELETE) `/api/items`(GET/DELETE) `/api/items/delete-sold` `/api/refresh`

## 合规声明

- 本项目用于监控**你自己**的商品，个人低频使用（默认 10 分钟一次、分页间随机延时）
- 请勿大规模采集他人数据或商用分发，遵守 Vinted 用户协议
- 仓库中所有密钥/代理地址均已替换为占位符，`server_config.json` 需自行填入你自己的代理池配置
