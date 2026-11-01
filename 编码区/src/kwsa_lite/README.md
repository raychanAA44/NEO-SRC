# KWSA lite · 单进程轻量网关

> 版本 v0.6.3 ｜ 默认端口 **8798** ｜ 纯标准库 ｜ 与现役 8888 并行运行，不改动 NEO 任何文件
>
> **独立交付件**：`<WORKSPACE_ROOT>/NEO LITE`（由 `python 运维区/scripts/build_lite.py` 生成，
> 自带 `core/` 与 `kwsa_lite/` 的完整副本、独立配置与 `运维区/data`，不引用 `NEO SRC/` 任何文件）。
> 下面「快速启动」描述的是**源码模式**；交付件直接双击 `启动 NEO lite.cmd`，无需关心源码路径。

KWSA lite 保留 HUB NEO 的数据面契约与路由架构，用十个 Python 模块 + 登录页/状态台
承载同样的网关效果：多渠道路由、渠道收录、模型探测、`auto/*` 分组、任务粘性锁、
决策端点改写、SSE 真流式、4 个转发头、trace 透传、JSON Access Log、令牌桶限流、
SQLite 账本、峰谷价目、输入/缓存/输出三段令牌、模型目录与路由自检。

v0.5.0 新增：面板自助改密、登录/改密/Key 安全审计、`/v1/*` 客户端 API Key
（只存 SHA-256 哈希 + 每 Key 独立令牌桶）、内网 TLS 终止反代脚本。

v0.6.1 / v0.6.2 新增：用量归因与两处 CSV、流水「仅失败」、渠道「设为默认」、KEY 接入串、模型别名；
价目按仓库快照与上游自报价补到在用模型 51/52，按实测停失效渠道/模型，裸模型名在停用渠道上回退到可用渠道；
发布改为 `python 运维区/scripts/deploy_lite.py`（检查 → 备份 → 重启 → 公网验收，`--rollback` 可回退）。

v0.6.3 新增（**停用原因可查**）：

- **为什么不可用**：模型目录上方一块「不可用 N / M」，按（渠道 × 原因码）分组，
  每组一行给出：原因码短标、一句话结论、影响模型数、上游实测证据（如 `HTTP 402 model_not_in_plan`）、
  恢复路径与实测时间。目录表状态列显示短码（`PLAN` / `DENIED` / `BLOCK` / `GONE` / `AUTH` / `LIMIT` / `MANUAL`），
  悬停看完整原因；表头新增「仅不可用」筛选。
- **复检即写回**：行内 `↻` 与分组「复检本组」会真打一次 1-token 推理（不打账本、不计入渠道健康），
  把结论写回停用注释——面板上的理由永远来自最近一次实测，不是手写备注。
- **原因码归一**：`classify_upstream_error()` 按上游真实回包归类（计划不含 / 拒绝此 Key / 风控拦截 /
  模型下线 / 鉴权失败 / 限流 / 网络不可达），每类自带恢复动作；拿不到报文只报「网络不可达」，不猜。
- **批量重查**：`python -m kwsa_lite.app --audit-models [--audit-channel X] [--audit-limit N]`
  对全部停用模型逐一实测并回写，换密钥或怀疑上游恢复后一条命令刷新。

v0.6.0 新增（面板可用性一轮）：

- **链路监视**：`/api/monitor` + 实测探针，把一次请求拆成 DNS / TCP / TLS / 首字节四段计时（真实请求，不造假数据）；呼吸灯块按健康状态变节奏。
- **价目可编辑**：面板直接写峰/谷单价与峰窗（`/api/control/pricing`）；流水新增「单价 IN/CA/OUT」列，按当次实际应用价显示。
- **三时区**：北京 / 美西 / 美东统一切换显示与用量分桶；峰窗同时换算三个时区。
- **路由矩阵可编**：分组成员增删（`/api/control/group`），成员以 chip 展示，可单点移除。
- **出口分路**：具名代理腿（`/api/control/egress`），可按渠道绑定或设全局默认；实测死腿会立刻失败，不会静默回落。
- **OC 免费档桥可选开关**：桥模式（推荐，转给 8898/8899 直连桥）与直连模式（lite 自补门禁：UA / session 四兄弟 / `read` 锚点 / 强制 stream / 剥 `tool_choice:none` / 非流式客户端自动聚合 SSE）。
- **用量查账**：1H / 24H / 7D / 30D 时间窗聚合，可切「渠道 / 模型」两种归因视图（模型视图按花费排序）与小时分布，`/api/usage/range`；当前视图可导出 CSV。
- **流水排障**：渠道 + 「仅失败」双筛选（`/api/usage?errors=1`），状态格悬停看错误原文；最近 30 条可导出 CSV。
- **模型别名**：渠道抽屉里按行写 `客户端名 = 上游名`，网关改写成上游模型并沿用本渠道（客户端拿 `gpt-4o` 这类硬编码名也能接）。
- **渠道行内操作**：渠道表直接编辑 / 启停 / 两步确认删除；模型目录支持搜索与模型级启停（停用的模型在数据面直接 403 `model_disabled`）；滑轨换成纸面同色，去掉系统白底。
- **任务锁面板**：路由矩阵下方列出 TTL 内的粘性锁（模型 · 剩余分钟），可单条 `×` 释放（`/api/locks/release`）。

> 云上部署（systemd `ProtectSystem=strict`）需要把 `运维区/configs` 也加入 `ReadWritePaths`，
> 否则面板的价目/渠道/出口写入会在只读沙箱里失败（v0.6.0 升级时已修正）。

**无预算闸门**：账本只记账，不按金额拦截请求。官方账单接口只保存登录参数，本版本不取数。

## 项目文档

- [文档索引](docs/README.md)：文档地图、现行实例快照、维护规则
- [01-项目说明](docs/01-项目说明.md)：定位、拓扑、模块、请求链路、与 NEO 的边界
- [02-部署与运维](docs/02-部署与运维.md)：首次启动、内网 TLS、Key 签发、备份、排障
- [03-安全与访问控制](docs/03-安全与访问控制.md)：面板认证、CSRF、数据面 Key、审计、残余风险
- [04-验收与版本](docs/04-验收与版本.md)：验收证据、复跑命令、校验和、版本演进

## 快速启动

```bash
cd "<PROJECT_ROOT>"

# 启动脚本自动复用 NEO 的 secrets 注入链（HUB_MODEL_TOKEN / HUB_OLLAMA_TOKEN）。
# 缺失时该渠道不带 Authorization（不会发空头）。
set HUB_MODEL_TOKEN=...        # Windows
set HUB_OLLAMA_TOKEN=...
# export HUB_MODEL_TOKEN=...   # bash

python 运维区/scripts/start_kwsa_lite.py
# 首次回环启动会生成一次性面板密码并打印到控制台；
# 也可以先设置自己的密码（推荐，至少 10 位）：
# python 运维区/scripts/start_kwsa_lite.py --set-password
# 打开 http://127.0.0.1:8798/ → 登录 →「安全」抽屉
# 首次启动生成的一次性密码只打印一次；登录后请在「安全」里改密，并清理旧日志。
# 数据面强制 Key：进入「安全 → 数据面 KEY」签发第一条，密钥只显示一次。
```

默认只绑 `127.0.0.1`。要给内网使用，推荐 **应用继续锁回环 + TLS 终止反代**，
不要把 8798 直接绑到 LAN：

```bash
# 1) 先生成/放置证书（SAN 必须是内网 IP 或域名）
# 2) 启动应用（保持 127.0.0.1:8798）
python 运维区/scripts/start_kwsa_lite.py
# 3) 只把 8443 暴露到内网
python 运维区/scripts/kwsa_lite_tls_proxy.py \
  --listen-host 192.0.2.11 --listen-port 8443 \
  --cert 运维区/certs/kwsa_lite_lan.crt \
  --key 运维区/certs/kwsa_lite_lan.key
# 打开 https://192.0.2.11:8443/
```

`kwsa_lite_tls_proxy.py` 固定转发到 `127.0.0.1:8798`，覆盖客户端传入的
`X-Forwarded-*`，保留原始 `Host` 供面板同源校验；应用看到
`X-Forwarded-Proto: https` 会自动给会话 Cookie 加 `Secure`（也可用
`auth.cookie_secure` 强制）。只有本机反代才允许 `auth.trust_proxy: true`，
非回环监听会拒绝该配置，避免伪造 `X-Forwarded-For` 绕过登录熔断。

也可以显式把应用绑到内网，但必须已有面板密码，且不要开 `trust_proxy`：

```bash
python 运维区/scripts/start_kwsa_lite.py --set-password
python 运维区/scripts/start_kwsa_lite.py --host 192.0.2.11
```

非回环监听而 `auth.enabled=false` 会直接拒绝启动；面板登录未启用时控制面仍只接受回环请求。

渠道密钥优先级：进程环境变量 → `运维区/data/kwsa_lite_creds.json` 加密密钥仓。
面板收录的密钥与官方登录密码只进密钥仓，配置文件中不落明文；接口只回掩码。

可选环境变量：

| 变量 | 作用 |
| :-- | :-- |
| `KWSA_LITE_CONFIG` | 配置文件路径（默认 `运维区/configs/kwsa_lite.json`） |
| `KWSA_LITE_PORT` / `KWSA_LITE_HOST` | 覆盖监听地址（默认 8798 / 127.0.0.1） |
| `KWSA_LITE_LEDGER_DB` | 覆盖 SQLite 账本路径 |
| `KWSA_LITE_VAULT` | 覆盖密钥仓路径（默认 `运维区/data/kwsa_lite_creds.json`） |
| `KWSA_LITE_RPS` / `KWSA_LITE_BURST` | 覆盖令牌桶速率 |
| `KWSA_LITE_RATE_LIMIT` | `0` 关闭限流（默认开启） |
| `KWSA_LITE_PROBE` | `1` 开启主动探针（默认由配置 `probe.enabled` 决定，当前 false） |
| `KWSA_LITE_OC_BRIDGE` | `1` 强制开启 OC 桥（默认由配置 `oc_bridge.enabled` 决定） |
| `KWSA_LITE_AUTH` | `0` 关闭面板认证；仅允许回环监听 |
| `KWSA_LITE_AUTH_USER` | 面板账号（默认 `admin`） |
| `KWSA_LITE_PASSWORD` | 部署侧下发面板密码；启动时对齐 scrypt 哈希，不落明文 |
| `KWSA_LITE_AUTH_FILE` | 面板凭据文件（默认 `运维区/data/kwsa_lite_auth.json`） |
| `KWSA_LITE_COOKIE_SECURE` | `1` 强制给会话 Cookie 加 `Secure`；TLS 反代下也会自动加 |
| `KWSA_LITE_TRUST_PROXY` | `1` 信任本机反代的 `X-Forwarded-For` 做登录熔断；仅回环监听可用 |
| `KWSA_LITE_REQUIRE_KEY` | `0` 临时关闭 `/v1/*` 客户端 Key 准入（默认强制） |
| `KWSA_LITE_KEYS_FILE` | 数据面 Key 仓路径（默认 `运维区/data/kwsa_lite_client_keys.json`） |
| `KWSA_LITE_AUDIT_FILE` | 安全审计 JSONL 路径（默认 `运维区/data/kwsa_lite_audit.jsonl`） |
| `KWSA_ADMIN_TOKEN` | 机器调用控制面的 Bearer 令牌；设置后免面板会话，仍需 `Authorization: Bearer` |

## 路由架构

```text
客户端 ──> :8798
             ├─ /v1/*  ── 客户端 API Key 准入 + 每 Key 令牌桶 ──> 路由裁决 ──> 渠道上游
             │              ├─ 具体模型：别名 / channel/model / custom/channel/model 直通
             │              └─ auto/* 分组：priority + smart_tier + 任务粘性锁
             ├─ /api/* ── 登录后的本地读模型（status / usage / channels / models / catalog / routes / official / locks）
             ├─ /api/control/* ── 登录后的控制面（渠道、模型、任务锁释放）
             ├─ /api/auth/* /api/security/* ── 改密、数据面 Key、安全审计
             └─ / ── 登录闸门 → 状态台 + 渠道收录抽屉 + 安全抽屉（数据来自 /api/status）
```

- **模型直通**：只有显式列出的模型才改道；未识别的模型走 `default_channel`，模型名零篡改。
- **分组**：`auto/*` 激活调度；`task_lock` 按 `x-hub-task-id`（或 `x-kwsa-task-id`）固化同一模型，TTL 12h。
- **决策渠道**：`endpoint=/v1/systemone` + `decision=true` 时，请求体改写为 decision 结构，缺 `state/questions` 直接 400。
- **数据面 Key**：`/v1/models` 与 `POST /v1/*` 都要求 `Authorization: Bearer <key>` 或 `X-API-Key: <key>`；Key 只存 SHA-256 哈希与前缀，创建时明文只返回一次。无活动 Key 时强制准入回 503 `client_key_missing`，不会静默放行。
- **每 Key 限流**：每个 Key 独立令牌桶（`rps` / `burst`），与全局按 IP 限流叠加；超限回 429 `key_rate_limited` + `Retry-After`，Access Log 带 `client_key` 名称。
- **控制面**：渠道/模型开关写回 `control` 段并原子落盘；`/v1/*` 闸门与读侧同源，不会出现“面板 OFF 还能打”。写操作要求面板会话 + 同源 `Origin/Referer`（或浏览器 `Sec-Fetch-Site: same-origin`）+ `X-KWSA-UI: 1`；`KWSA_ADMIN_TOKEN` 可用 Bearer 免会话。
- **渠道收录**：`action=save` 一次写入渠道、模型、密钥/登录凭据与开关；`action=probe` 只读上游 `/v1/models`，不落盘；`action=delete` 同时清掉 `control`、分组、价目与密钥仓记录。
- **峰谷价**：`pricing.channels.<渠道>.peak_rule` 声明峰窗；`in/cache/out` 是峰价，`off_in/off_cache/off_out` 是谷价。无 `peak_rule` 时按单一价。每笔流水落 `price_period` 与当次应用单价。
- **令牌**：账本、状态台和流水都拆 `prompt_tokens`（IN）/ `cached_tokens`（CACHE）/ `completion_tokens`（OUT）；缓存命中量只按 `cache` 价计一次。

## 配置要点

见 `运维区/configs/kwsa_lite.json`：

- `auth`：`enabled`（默认 `true`）、`username`、`session_ttl_s`（默认 12h）、`cookie_secure`、`login_max_failures`（默认 5）、`login_window_s`（默认 300）、`trust_proxy`（只允许回环监听）。口令只以 scrypt 哈希落 `运维区/data/kwsa_lite_auth.json`；会话在内存，进程重启即失效。
- `data_plane`：`require_key`（默认 `true`）、`keys_path`、`default_rps`（默认 20）、`default_burst`（默认 40）。Key 明文只在签发响应出现一次，落盘只有 SHA-256 哈希。
- `audit`：`path`（默认 `运维区/data/kwsa_lite_audit.jsonl`）、`max_bytes`（默认 2 MiB，超限滚动为 `.1`）。只记事件、账号、客户端 IP、备注与 UA，不记口令/会话/Key。
- `channels.<name>`：`base_url`、`token_env`、`models`、`aliases`、`endpoint`、`decision`、`probe_path`、`headers`（值支持 `env:VAR`）。
- `channels.<name>.login_url` / `usage_path`：官方账单登录地址与账单路径，只做接口预留；账号密码进密钥仓，配置文件不落明文。
- `egress`：`enabled`、`default`（腿 id，空 = 直连）、`legs[]`（`id` / `name` / `url` / `enabled` / `note`）。只接受 `http(s)` 代理；渠道级 `channels.<name>.egress` 优先于全局默认，腿被停用即回落直连。
- `oc_bridge`：`enabled`、`mode`（`bridge` 打外部直连桥 / `direct` 自补门禁）、`channel`（虚拟渠道代号，默认 `oc-bridge`）、`bridge_url`、`upstream`、`models`、`user_agent`、`egress`、`token_env`。开启时注入一个 `virtual=oc` 的渠道，关闭即下线；虚拟渠道不能从渠道表删除或改写。桥 token 只进密钥仓（条目名 `oc_bridge`）。
- `groups`：`policy.task_lock` / `smart_tier`，成员按 `priority` 升序。
- `pricing`：`unit=USD_per_mtok / CNY_per_mtok`、`usd_cny`；渠道块可写 `peak_rule` 与 `models`。模型行 `in/cache/out` 是峰价，`off_in/off_cache/off_out` 是谷价；`$note` 原样展示在状态台，当前为“示意价目 · 上线前按渠道替换”。
- `control`：只剩渠道与模型开关，无 `budgets`。
- `probe.enabled`：主动探活（默认关闭；关闭时状态台显示 `WAIT`，有真实请求后按被动结果更新）。

## 端点

| 方法 | 路径 | 说明 |
| :-- | :-- | :-- |
| GET | `/health`、`/healthz` | 公开最小心跳：运行态、端口、默认渠道、契约自检；不含渠道明细/账本/配置路径 |
| GET | `/metrics` | Prometheus 文本（不计限流） |
| GET | `/api/session` | 当前会话：`authenticated` / `user` / `auth_enabled`，未登录也回 200 |
| POST | `/api/login` | `{username, password}`；成功写 HttpOnly 会话 Cookie，失败 401，连续失败 429 |
| POST | `/api/logout` | 撤销当前会话并清 Cookie |
| GET | `/api/status` | **需登录**；状态台一次取全量：通道 / 今日 / 目录 / 官方预检 / 最近 8 条 |
| GET | `/api/usage/today`、`/api/usage?limit=&channel=&status=&errors=1` | **需登录**；今日聚合 / 流水（含峰谷与 IN/CA/OUT，`errors=1` 只取失败） |
| GET | `/api/channels`、`/api/models`、`/api/routes`、`/api/groups`、`/api/locks` | **需登录**；观测面 |
| GET | `/api/catalog` | **需登录**；模型目录 + 峰价/谷价 + 路由自检 |
| GET | `/api/official/sources` | **需登录**；官方账单参数完备度；`connected=false`，不取数 |
| GET | `/api/official/usage` | **需登录**；官方用量占位：固定 `source=none`，不拿 0 冒充 |
| GET | `/api/pricing`、`/api/control` | **需登录**；价目与控制快照 |
| GET | `/api/monitor` | **需登录**；中间层样本（路由/上游/首字节/总耗时分位）+ 渠道健康 + 出口腿 |
| GET | `/api/usage/range?tz=&span=&from=&to=` | **需登录**；时间窗聚合（渠道/模型/日/小时），`tz` 为秒偏移（北京 28800 / 美西 -28800 / 美东 -14400） |
| POST | `/api/auth/password` | **需登录 + 同源 + UI 头**；`{current_password, new_password, confirm_password}`；成功后撤销全部旧会话并回新 Cookie |
| GET | `/api/security/keys` | **需登录**；数据面 Key 列表（只回 id/name/prefix/速率/最近使用，不回哈希与明文） |
| POST | `/api/security/keys` | **需登录 + 同源 + UI 头**；`{action:"create", name, rps?, burst?}` 返回一次性 `secret`；`{action:"toggle", id, enabled}`；`{action:"revoke", id}` |
| GET | `/api/security/audit?limit=&event=` | **需登录**；登录/退出/改密/Key 生命周期审计，尾部读取 |
| GET | `/v1/models` | OpenAI 兼容模型清单；**需数据面 Key**；只列可用模型（停用的渠道/模型不播报，面板目录仍全量） |
| POST | `/v1/*` | 推理反代（SSE 逐块透传）；**需数据面 Key** |
| POST | `/api/control/channel` | 开关：`{name, enabled}`；收录：`{action:"save", name, base_url, token?, token_env?, models, login_url?, usage_path?, login_user?, login_password?, clear_login?, enabled?, make_default?}`；探测：`{action:"probe", base_url, token?}`；删除：`{action:"delete", name}`；设默认：`{action:"default", name}`；忘密钥：`{action:"forget_token", name}` |
| POST | `/api/control/model` | `{channel, model, enabled}` |
| POST | `/api/control/pricing` | `{action:"save", channel, model, in, cache, out, off_in?, off_cache?, off_out?, note?}`；`{action:"delete", channel, model}`；`{action:"peak_rule", channel, rule}`（`rule={kind:"utc_weekday_window", start_hour, end_hour}` 或 `{kind:"flat"}`） |
| POST | `/api/control/group` | `{action:"add_member", group, channel, id, tier?, priority?}`；`{action:"remove_member", group, channel, id}`；`{action:"policy", group, policy}`；`{action:"delete_group", group}` |
| POST | `/api/control/egress` | `{action:"save", id, url, name?, enabled?}`；`{action:"delete", id}`；`{action:"default", id}`；`{action:"channel", channel, id}` |
| POST | `/api/control/oc` | `{action:"save"\|"enable"\|"disable", mode, channel, bridge_url, upstream?, models[], token?}`；`{action:"probe"}` 真实探一次桥/上游 |
| POST | `/api/control/monitor` | `{action:"probe", channel}`：DNS/TCP/TLS/TTFB 分段实测；`/v1/models` 回退 |
| POST | `/api/locks/release` | `{task_id, group_id}` |

控制面要求：面板会话 + 同源 `Origin/Referer`（或 `Sec-Fetch-Site: same-origin`）+ `X-KWSA-UI: 1`；或 `Authorization: Bearer $KWSA_ADMIN_TOKEN`。
`auth.enabled=false` 时只允许回环 + `X-KWSA-UI: 1`（仅本机开发）。
数据面要求：`Authorization: Bearer <client key>` 或 `X-API-Key: <client key>`；面板会话不能代替数据面 Key。
其余 `POST /api/*` 一律 403；`/api/admin/*`、`/api/actuator/*`、`/api/vault` 一律 403。

## 与 NEO 的边界

保留：反代、渠道收录/探测/删除、别名、分组、任务锁、决策端点、SSE、转发头、trace、限流、
JSON 日志、SQLite 账本、峰谷价目、令牌三段、模型目录、路由自检、单实例、探针、状态台、
面板登录/会话/CSRF/登录熔断、数据面 Key/每 Key 限流、安全审计、TLS 反代、回环开发模式。

不包含：预算闸门、官方账单取数、账号池 / 签到、KEY 保险库、提示词自愈、SSE 补帧、
工具命令审计、上下文折叠、HTTP/2、多实例共享账本。官方登录参数已可保存，但本版本不发起取数；
需要实际官方账单时仍由 8888 承担。

## 面板安全与数据面 Key

1. 登录后点报头「安全」打开安全抽屉；面板口令区输入当前口令 + 新口令（至少 10 位）→ 更新。成功后其它会话立即失效，当前浏览器自动换新 Cookie。
2. 「数据面 KEY」区填名称、RPS、BURST → 签发；明文只显示一次，可复制后立即用于客户端。
3. Key 可停用/启用/吊销；吊销立即生效。强制准入开启且没有活动 Key 时，`/v1/*` 回 503，不会退化成开放。
4. 审计区可按事件筛选登录成功/失败/熔断、退出、改密成功/失败、Key 签发/启停/吊销；只读尾部，不把大日志全量加载。

命令行签发自检（明文只打印一次）：

```bash
python 运维区/scripts/start_kwsa_lite.py --create-client-key lan-console --key-rps 20 --key-burst 40
```

## 渠道收录

1. 打开状态台，点「＋ 收录渠道」。
2. 填渠道代号、上游 `base_url`、密钥（或环境变量名）；点「探测模型」读上游 `/v1/models` 并回填模型清单。
3. 官方账单登录是独立折叠区：填登录地址、账单路径、账号、密码。保存后只落加密密钥仓，不发起请求。
4. 保存后配置热加载，不重启；新模型可直接用 `模型名` / `渠道/模型` / `custom/渠道/模型` 调用。
5. 勾「设为首选渠道」可切换未识别模型的默认落点；勾「启用该渠道」控制 `/v1/*` 闸门。

安全边界：密钥与官方登录密码只进 `kwsa_lite_creds.json` 密文仓，密钥文件 `kwsa_lite_creds.key` 独立；
标签不匹配会显式报错，不返回半截明文。自定义头只接受 `env:VAR`，避免明文落盘。

## 测试

```bash
python -m pytest 编码区/tests/test_kwsa_lite.py -q     # 62 项
python 编码区/scripts/run_tests.py                     # 全量回归（含 KWSA lite）
```
