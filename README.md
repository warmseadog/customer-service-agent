# 客服邮件工作台

> **被动入站客服邮件 Agent**：自动接收用户来信，分析情绪与语气，生成安抚或普通客服回复，判断是否需要升级并向产品负责人发送内部通知邮件。系统不发送任何主动外呼邮件。

---

## 核心流程

```
用户来信（IMAP 拉取）
       ↓
   匹配绑定产品（线程已有 / 关键词匹配兜底）
       ↓
   LLM 分析情绪 & 语气
   ├─ sentiment: satisfied / neutral / dissatisfied
   └─ tone: cooperative / firm / hostile
       ↓
   升级决策（混合策略）
   ├─ LLM 推荐升级
   ├─ 规则强制：律师 / 投诉 / 媒体等关键词
   └─ 同线程二次不满意（在配置时间窗内）
       ↓
   生成对外回复（安抚 or 普通）
   │   安抚/升级场景下是否声称「已联系售后」由处理结果与提示词保证（非 `AUTO_REPLY_ON_ESCALATION` 开关，见下表）
       ↓
   SMTP send_reply → 用户收到回信
       ↓（若需升级）
   向产品负责人发送内部升级通知邮件
   记录 escalation_events
```

**性能与并发（简述）**：每轮先 **并行 IMAP** 拉取各邮箱未读，再按 **邮件会话**（含 `mailbox_id`，见 `thread_scope.py`）分组；**不同会话**可并行处理（`EMAIL_PROCESS_MAX_WORKERS`），**同一会话多封未读**仍顺序处理。日志中输出各阶段耗时，便于调优。

---

## 功能概览

| 功能 | 说明 |
|---|---|
| **情绪与语气分析** | 每封来信自动分析 sentiment（满意/中性/不满意）和 tone（配合/强硬/敌对） |
| **安抚回复生成** | 不满/强硬/敌对时发安抚信：**语气 hostile** 用高共情、优先道歉 + 必索信息；**配合/强硬** 用专业克制、正常询证 + 必索信息 |
| **普通客服回复** | 满意/中性+配合时直接回答问题；**满意**时在结尾极委婉提示「若愿意分享体验可帮助他人参考」（零施压） |
| **安抚少问策略** | 已识别到产品时，安抚信**必索订单号/凭证**，问题描述**非必索**；已提供的信息不重复追问；**多轮后**（我方回信数 ≥ `CALM_EMPATHY_ONLY_MIN_PRIOR_OUTBOUND`，默认 3）对客户**不重复**售后时间线空话，短篇共情为主（`empathy_pure`） |
| **全局兜底收件人（团队侧）** | `DEFAULT_SUPPORT_OWNER_EMAIL`（.env）或仪表盘**「内部客服」**页内配置；未命中产品或产品无负责人时作为兜底 |
| **内部通知路由** | 安抚类先发内部通知再回用户；一般不满→**产品 owner 链**；情绪激烈（hostile）→直接送达**产品 owner 链或全局兜底收件人**；含摘要与原文节选 |
| **升级冷却保护** | 同线程升级邮件在冷却时间内不重复发送，避免刷屏 |
| **产品匹配** | 线程已绑定产品优先使用，否则用关键词匹配产品库兜底 |
| **多轮对话记忆** | 按邮件线程（Thread）隔离存储完整收发历史，最多送 LLM N 条 |
| **Web 仪表盘** | 客户会话、内部通知、客诉分析、产品库、**内部客服**（可分配负责人名单 + 备用/兜底收件人）、日志 |
| **多邮箱收件** | 在仪表盘「邮箱账户」配置多台；每轮 **IMAP 并行拉取**（`MAILBOX_FETCH_MAX_WORKERS`）；**不同邮件会话**可并行处理（`EMAIL_PROCESS_MAX_WORKERS`），同一会话内多封未读仍顺序处理 |
| **阶段耗时日志** | 每轮结束输出 **本轮阶段耗时**：IMAP 拉取 / 分拣组批 / 处理来信；每封来信结束输出 **本封耗时**：准备与入库来信、入站分析图、内部升级、安抚或兜底正文、回复客户 SMTP、意图落库及「其它」余项，便于定位瓶颈 |

---

## 快速启动

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，填入邮箱密码、LLM API Key、DEFAULT_SUPPORT_OWNER_EMAIL 等

# 3. 启动服务（自动初始化数据库）
python -m app.main
# 或：
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 4. 打开登录页并登录（首次启动需在 .env 配置 AUTH_BOOTSTRAP_* 创建管理员，见下节）
# http://localhost:8000/login
# 登录成功后进入：http://localhost:8000/dashboard
```

### 仪表盘登录与权限（HTTPS + Cookie）

- **入口**：浏览器访问 [`/login`](http://localhost:8000/login)，使用用户名与密码登录；会话保存在 **HttpOnly** Cookie（名称由 `AUTH_SESSION_COOKIE` 配置，默认 `cs_session`），前端 API 请求需携带 Cookie（仪表盘已使用 `credentials: 'include'`）。
- **首个管理员**：数据库中 **没有任何用户** 时，若 `.env` 设置了 `AUTH_BOOTSTRAP_ADMIN_USER` / `AUTH_BOOTSTRAP_ADMIN_PASSWORD`，进程启动时会自动创建该 **admin** 账号。已有用户后请通过管理员调用 `POST /auth/users` 等方式建号，勿依赖 bootstrap。
- **角色**：`admin`（全量，含危险批量删除与用户管理）、`operator`（读写业务数据，不含批量删库/删全产品等）、`viewer`（只读；仪表盘上隐藏保存/删除类按钮。**可选**：为该账号勾选绑定邮箱 → 仅能只读这些数据；若不绑定仍为全站只读）。管理员登录后可在仪表盘 **「账号管理」** 页新建用户、改角色/启用状态/重置密码、删除用户（不可删除当前登录账号）。
- **HTTPS**：公网部署时由 Nginx/Caddy 等终结 TLS，并设置 `X-Forwarded-Proto: https`（或 `Forwarded`）。生产环境将 **`AUTH_COOKIE_SECURE=true`**，否则浏览器拒绝在 HTTPS 下发送 `Secure` Cookie。本地 HTTP 调试保持 `AUTH_COOKIE_SECURE=false`。
- **静态页与接口**：`GET /dashboard` 返回仪表盘 HTML（便于未登录时由前端跳转登录）；**所有 JSON API**（除 `POST /auth/login`、公开 `GET /` 等）均需有效会话。

---

## 项目结构

```
.
├── app/
│   ├── agent.py              # 来信处理核心：收信→分析情绪→升级判断→发回复→写升级记录（含分段耗时日志）
│   ├── config.py             # 环境变量统一读取（含客服配置项；字符串 trim）
│   ├── thread_scope.py       # 线程 ID 作用域：mailbox_id + RFC 线索，避免多收件箱键冲突
│   ├── escalation_settings.py # 全局兜底收件人：DB 覆盖层 + effective 取值
│   ├── database.py           # SQLite 持久化层
│   ├── llm_service.py        # LLM 调用：情绪分析 + 回复生成 + 升级摘要
│   ├── mail_service.py       # IMAP 收件 + SMTP 发件（用户回复 & 内部升级通知）
│   ├── auth_service.py       # 登录、bootstrap、密码与会话
│   ├── auth_deps.py          # FastAPI Depends：当前用户与角色
│   ├── main.py               # FastAPI 入口 + 仪表盘路由
│   ├── graphs/
│   │   └── inbound_graph.py  # LangGraph 客服状态机（5 节点）
│   ├── services/
│   │   ├── lead_service.py      # 客诉分析查询（意图识别结果）
│   │   └── product_service.py   # 产品库管理（含 owner 字段）
│   └── web/
│       ├── dashboard.html    # 单页 Web 仪表盘
│       └── login.html        # 登录页
├── data/
│   └── products.json         # 本地产品库（初始种子数据，含 owner_name/owner_email）
├── .env                      # 实际配置（不提交 Git）
├── .env.example              # 配置模板
├── kol_agent.db              # SQLite 数据库（自动创建）
└── requirements.txt
```

---

## 数据库表结构

系统使用 SQLite，连接使用 **WAL** 与 **busy_timeout**（见 `database._get_conn`），便于多会话并行写入时降低锁等待。核心表包括（另含历史兼容表，见下行）：

| 表名 | 职责 |
|---|---|
| `creators` | 联系人主档：邮箱、姓名等；**来信处理时会自动 upsert**，仪表盘不提供手工增删客户 |
| `products` | 产品库：名称、**brand（品牌）**、**asin**、关键词、**负责人 owner_name / owner_email** |
| `mailboxes` | 邮箱账户：IMAP/SMTP、对外品牌与发件人名；多账户轮询收信 |
| `mailbox_products` | **产品与邮箱多对多**：仅当某产品关联到某邮箱时，该邮箱的来信关键词匹配才会命中该产品 |
| `support_staff` | 内部可分配人员：姓名、邮箱；**仪表盘「内部客服」名单**与产品负责人下拉数据源 |
| `kol_threads` | 邮件线程状态：情绪标签、最后处理消息、绑定产品；**thread_id 含邮箱作用域**（与 `thread_scope` 一致） |
| `thread_messages` | 多轮对话历史：每封来信和我方回复 |
| `intent_results` | 每封来信的情绪识别结果（cs_sentiment / cs_tone / escalated） |
| `escalation_events` | 升级事件记录：通知发送至、升级原因、时间戳 |
| `processed_messages` | 已处理邮件去重记录 |
| `support_escalation_settings` | 单行：仪表盘覆盖的全局 BACKUP / DEFAULT 收件人（邮箱为空则该字段仍用 .env） |
| `users` | 仪表盘登录账号：用户名、`password_hash`、角色 `admin`/`operator`/`viewer`、是否启用 |
| `user_sessions` | 服务端会话：随机 token、用户 FK、过期时间；登出或过期后失效 |
| `campaigns` / `outreach_messages` / `tickets` | 保留表结构（历史数据兼容），不再写入新数据 |

### `escalation_events` — 升级事件

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | 自增主键 |
| thread_id | TEXT | 邮件线程 Key |
| creator_id | INTEGER | 关联联系人 |
| product_id | TEXT | 关联产品 |
| reason | TEXT | 升级触发原因（LLM 推荐 / 关键词 / 二次不满意） |
| internal_email_to | TEXT | 内部升级通知发送至（产品 owner 或全局默认） |
| sent_at | TEXT | 发送时间 |
| created_at | TEXT | 记录创建时间 |

### `products` — 产品与负责人字段（仪表盘维护）

| 字段 | 说明 |
|---|---|
| `brand` | 品牌名称（可选；不同产品可属不同品牌，用于话术与展示「品牌 · 品名」） |
| `owner_name` | 产品负责人姓名 |
| `owner_email` | 升级通知首选收件邮箱 |
| `fallback_owner_email` | owner_email 为空时的备用邮箱 |

---

## 产品绑定与负责人（1A）

**规则**：产品的 `owner_email` 是升级通知的主要收件人来源，仅通过以下方式维护：

1. 编辑 `data/products.json` 后重新初始化数据库（首次空库自动 seed）
2. 通过仪表盘「产品库」Tab 的表单更新（`POST /products`）。负责人可从「**内部客服**」页维护的名单（`support_staff`）下拉选择，也可选「其他」手动填写内部邮箱
3. 直接修改 SQLite 数据库中 `products` 表的 `owner_email` 字段

**独立站邮箱 ↔ 产品（多对多）**：在「产品库」勾选「独立站邮箱」后保存，会写入 `mailbox_products`。系统对某封来信的**关键词自动绑品**只在「当前收件邮箱所关联的产品集合」内匹配，**不会**扫全库；未勾选任何邮箱的产品不参与任何邮箱的关键词匹配。请求体带 `mailbox_ids` 时会**覆盖**该产品的全部邮箱关联；不带该字段则**不改**现有关联（兼容旧客户端）。

**产品库界面**：

- **按邮箱看产品**：列表上方有「**全部产品** / **各邮箱**」**下划线切换**（选中状态存入浏览器 `sessionStorage`，刷新可保留）。点某一邮箱后，表格**只显示**已关联到该邮箱的产品（按站管理，来信关键词只在这些产品里匹配）；点「全部产品」时多一列 **「邮箱账户」** 摘要。
- **放大镜 · 筛选邮箱标签**：邮箱标签行右侧有搜索按钮，展开后按 **名称 / 邮箱 / ID** 过滤**标签按钮**（「全部产品」与**当前已选邮箱**的标签不会被筛掉，避免找不到选中项）。
- **放大镜 · 产品表单里的独立站邮箱**：勾选多选列表旁可展开搜索，按关键词**显示/隐藏**各行复选框，便于几十个邮箱时快速勾选。
- **保存**：在某一邮箱视图下保存产品时，会自动把**当前视图邮箱**并入 `mailbox_ids`（与已勾选项合并去重）。

**客户会话界面**：

- **当前邮箱**下拉旁有 **放大镜**：展开后按关键词**筛选下拉中的邮箱选项**（「全部」保留；若当前选中项被筛掉，仍会出现在列表中以免丢失选择）。

**内部名单**：在「内部客服」页添加/删除人员会调用 `POST /support-staff`、`DELETE /support-staff/{id}`，与产品表单中的负责人下拉实时一致。

一般（非 hostile 的安抚类、及非安抚类升级）收件人优先级：`product.owner_email` → `product.fallback_owner_email` → **BACKUP** → **DEFAULT**。

**例外**：安抚类且 **tone = hostile** 时，内部通知**先**发 **BACKUP**（无则仍按上列链），便于集中处理高冲突工单。

**不提供**：线程级别的负责人切换 UI、`/products/{id}/owner` 独立 API、邮件密语绑定。

---

## 内部升级邮件说明

升级邮件主题格式：`[客服升级·{推送次序}] {联系人姓名} — {产品名称}`，其中「推送次序」为 `初次推送` / `第二次推送` / `第N次推送`（同会话内多次发给内部的升级通知）。

邮件内容包含：
- 【推送次序】说明（同上，便于区分本会话第几封内部同步）
- 线程 ID（会话排障）
- 联系人姓名与邮箱
- 绑定产品名称
- 优先级建议（high / medium）
- LLM 生成的 ≤5 行摘要（及可选的用户原文节选、中文译文段落等，见 `mail_service.send_internal_escalation`）
- 【客服升级通知】正文摘要

**升级冷却**：同一线程在 `ESCALATION_EMAIL_COOLDOWN_MINUTES` 分钟内不重复发送升级通知（防止刷屏）。安抚路径下发内部通知时可经 `CALM_BYPASS_ESCALATION_COOLDOWN`（默认开启）跳过冷却，避免与用户侧可见话术（如「已联系售后」）不同步。

---

## 仪表盘说明

访问 `http://localhost:8000/dashboard`

| 标签页 | 内容 |
|---|---|
| **客户会话** | 会话列表 + 最近情绪预览。**当前邮箱**：下拉筛选本会话列表来自哪一台收件箱；旁侧 **放大镜** 可按关键词筛下拉选项（名称/邮箱/ID）。 |
| **内部通知** | 已发给产品/备用的内部说明邮件留痕：客户、产品、事由、通知发送至 |
| **客诉分析** | 每封来信的 sentiment / tone / 是否已转内部 |
| **产品库** | 产品增删改（1A）。**按邮箱下划线切换**仅看该站关联产品；**放大镜**可搜邮箱标签 / 搜表单里的多选邮箱。**在某一邮箱视图下保存**会自动带上该邮箱到 `mailbox_ids` |
| **内部客服** | **同一页**包含：① **可分配负责人名单**（姓名、内部企业邮箱，对应 `support_staff`）；② **全局兜底收件人**（与 `GET/PUT /settings/escalation` 一致）。编辑区为 **一行两列**：「兜底 · 邮箱 | 兜底 · 称呼」，少占纵向空间 |
| **邮箱账户** | 多台站点邮箱：分别填写 IMAP/SMTP；仪表盘含阿里云企业邮、Gmail、Outlook/Hotmail、Microsoft 365、Yahoo、网易 163 等一键模板。**「测试」**：依次验证 **IMAP**（登录并预览最多 1 封未读，可无未读）与 **SMTP**（与同账号真实发信相同的 TLS/登录流程，**仅 AUTH，不投递邮件**）。对外客服回复与内部升级通知共用该邮箱的 SMTP |
| **运行日志** | 最近 200 条运行日志（接口 `GET /logs?tail=200`），每 6 秒自动刷新 |

进程启动后**默认不自动开启**收件轮询（`AUTO_START_POLLING` 默认为 `false`）；需在仪表盘点击「启动轮询」或调用 `POST /start-auto`。设为 `true` 则启动进程后即按间隔后台轮询；停止请点「停止轮询」或结束进程。详见 **[运行日志与耗时](#运行日志与耗时)**；若 `LLM_API_KEY` 为空，`call_llm` 会抛出明确错误。

**说明**：已不再提供「来函客户」类手工客户表维护；`creators` 仅由收信流程按需写入，用于会话与升级展示关联。

---

## 运行日志与耗时

- **内存环形缓冲**：`GET /logs?tail=N`、仪表盘「运行日志」展示最近若干条。
- **每一轮检查**（`run_check_cycle`）：日志行 **⏱ 本轮阶段耗时** 拆分为 **合计**、**IMAP 拉取**、**分拣组批**（邮箱状态更新 + 按 `thread_scoped` 分组排序）、**处理来信**（并行上限见 `EMAIL_PROCESS_MAX_WORKERS`），并附带本轮 IMAP / 处理 worker 配置说明。
- **每一封来信**：在 **⏱ 本封耗时** 中拆分为 **准备+入库来信**、**入站分析图**（LangGraph）、**内部升级**（进入升级通知路径时的译文/摘要/SMTP）、**安抚/兜底正文**（安抚路径的 `generate_calm_reply`，非安抚则为 0）、**回复客户**（对外 SMTP）、**意图落库**、**其它**（未计入前项的间隔，正常应接近 0）。

并行处理时多线程日志可能交错，属于正常现象。

---

## API 端点

| 端点 | 说明 |
|---|---|
| `GET /dashboard` | Web 仪表盘 |
| `POST /check` | 立即触发一轮来信检查；若上一轮检查（含后台定时轮询中的 `run_check_cycle`）仍在执行，`asyncio.Lock` 未释放，返回 **409 Conflict**：`已有检查任务正在后台运行，请勿重复点击` |
| `POST /start-auto` | 启动后台定时轮询；若已在运行则返回 `already_running` |
| `POST /stop-auto` | 停止后台轮询（停止后需再次调用 `/start-auto` 才会恢复） |
| `GET /status` | 服务状态（含 `auto_polling`、`auto_start_polling_default`、`poll_interval_seconds`、汇总数据等） |
| `GET /settings/escalation` | 全局 DEFAULT：数据库覆盖值、生效值、.env 对照（body 与仪表盘表单项一一对应） |
| `PUT /settings/escalation` | 保存全局收件至数据库（`default_owner_email` / `default_owner_name`；空字符串表示清除该字段覆盖并回退 .env） |
| `GET /products` | 产品列表（含 owner、**`mailbox_ids`** 等；与 `mailbox_products` 关联一致） |
| `GET /support-staff` | 内部可分配人员列表（产品负责人下拉数据源） |
| `POST /support-staff` | 添加人员（body: `display_name`, `email`） |
| `DELETE /support-staff/{id}` | 删除人员记录 |
| `POST /products` | 新增/更新产品（owner 维护入口）。可选 **`mailbox_ids`**（整数数组）：传入则**覆盖**该产品全部邮箱关联；不传则**不改动**现有关联 |
| `DELETE /products/{id}` | 删除产品 |
| `GET /intents` | 情绪识别结果列表（含 cs_sentiment / cs_tone / escalated） |
| `DELETE /intents/{intent_id}` | 删除单条情绪识别记录 |
| `DELETE /intents` | 清空全部情绪识别记录 |
| `GET /escalations` | 内部通知（escalation）记录列表 |
| `DELETE /escalations/{escalation_id}` | 删除单条内部通知留痕 |
| `DELETE /escalations` | 清空全部内部通知留痕 |
| `GET /kols` | 客户会话概览；可选查询参数 **`mailbox_id`** 仅看该收件箱 |
| `GET /thread/{thread_id}` | 单一会话完整对话历史 |
| `DELETE /thread/{thread_id}` | 删除该会话数据 |
| `DELETE /threads` | 清空全部会话数据 |
| `GET /processed` | 已处理邮件去重记录列表（调试/运维） |
| `GET /logs?tail=200` | 最近运行日志（`tail` 为条数上限，内存环形缓冲） |
| `DELETE /logs` | 清空当前进程内存中的运行日志缓冲区 |
| `DELETE /products` | 删除全部产品（慎用） |
| `DELETE /all-data` | 清空全部业务相关数据（慎用） |
| `GET /emails` | 预览当前未读邮件（不触发处理） |
| `GET /mailboxes` | 邮箱账户列表 |
| `POST /mailboxes` | 新增邮箱账户 |
| `PUT /mailboxes/{mailbox_id}` | 更新邮箱账户 |
| `DELETE /mailboxes/{mailbox_id}` | 删除邮箱账户 |
| `POST /mailboxes/{mailbox_id}/test` | 连通性测试：**IMAP** + **SMTP 登录**（不发信）。响应含 `status`: `ok` / `partial` / `failed`，以及 `imap` / `smtp` 分项与兼容字段 `peek_count` |

---

## 配置项速查

| 变量 | 默认值 | 说明 |
|---|---|---|
| `EMAIL_ADDRESS` | — | 阿里企业邮箱地址 |
| `EMAIL_PASSWORD` | — | 邮箱密码 |
| `SENDER_DISPLAY_NAME` | Support Team | 对外 `From` 显示名 |
| `MAIL_REPLY_SUBJECT_WEB_STYLE` | true | `true` 时回复主题为阿里网页风「回复：…」，`false` 时为 `Re:` 风格 |
| `IMAP_HOST` / `IMAP_PORT` | imap.qiye.aliyun.com / 993 | 一般保持默认即可 |
| `SMTP_HOST` / `SMTP_PORT` | smtp.qiye.aliyun.com / 465 | 同上 |
| `LLM_API_KEY` | — | LLM 服务 API Key（OpenRouter 多为 `sk-or-v1-…`）。**须写入已保存的项目根目录 `.env`**；仅在编辑器里填写未保存到磁盘时，进程读不到密钥，易出现 **401 / Missing Authentication**。 |
| `LLM_BASE_URL` | `https://openrouter.ai/api/v1` | Chat Completions 兼容地址（OpenRouter / OpenAI / 其它兼容网关）；须与密钥所属服务商一致 |
| `LLM_MODEL` | `google/gemini-3.1-pro-preview` | OpenRouter **模型 slug**（如 `qwen/qwen3.6-plus`，勿使用路由上不存在的名称） |
| `LLM_TIMEOUT` | 60 | LLM 请求超时（秒） |
| `LLM_HTTP_REFERER` / `LLM_APP_TITLE` | — | OpenRouter 可选自愿头；留空则不发送（见 `.env.example`） |
| `BRAND_NAME` | Our Brand | 品牌名（注入 LLM prompt） |
| `BRAND_SIGNATURE` | Support Team | 邮件署名 |
| `HOST` / `PORT` | 0.0.0.0 / 8000 | HTTP 服务监听地址与端口 |
| `DEFAULT_SUPPORT_OWNER_EMAIL` | — | **最后一级兜底**：产品无负责人或未匹配到产品时的升级通知收件人 |
| `DEFAULT_SUPPORT_OWNER_NAME` | Support Owner | 全局默认负责人姓名 |
| `AUTO_REPLY_ON_ESCALATION` | true | **预留**，当前未接入业务逻辑；对外话术由安抚流 `after_sales_notified` 与提示词控制 |
| `ALLOW_COMPENSATION_PROMISES` | false | 是否允许 LLM 在回复中承诺具体赔偿 |
| `REPEAT_DISSATISFACTION_HOURS` | 24 | 判断「二次不满意强制升级」的时间窗（小时） |
| `ESCALATION_EMAIL_COOLDOWN_MINUTES` | 60 | 同线程升级邮件冷却时间（分钟） |
| `CALM_BYPASS_ESCALATION_COOLDOWN` | true | 不满/安抚类首次对外声称「已联系售后」等场景是否忽略冷却，保证内部通知必达 |
| `CALM_EMPATHY_ONLY_MIN_PRIOR_OUTBOUND` | 3 | **安抚模式 empathy_pure 触发阈值**：本条回信**之前**线程里我方已回信条数 ≥ 该值（且走安抚路径）时，对客户**不再重复**售后/时间线套话，以短共情为主；夹在 **1～32** |
| `DB_FILE` | kol_agent.db | SQLite 路径；**相对路径相对项目根目录**解析，与从哪个目录启动进程无关 |
| `PRODUCTS_PATH` | data/products.json | 同上，相对项目根 |
| `SUPPORT_STAFF_PATH` | data/support_staff.json | 同上，相对项目根 |
| `MAX_THREAD_MESSAGES` | 10 | 每线程送 LLM 的最大历史条数 |
| `BODY_EXCERPT_LENGTH` | 600 | 邮件正文入库截断字符数 |
| `POLL_INTERVAL` | 120 | **轮询间隔（秒）**。每跑完一整轮检查后休眠该时长再起下一轮：**新来信的理论最大等待时间约等于本轮间隔 + 当周期间隔内尚未开始轮询的排队时间**（另受单轮耗时、来信量影响）。可调小（如 **60**）以加快响应；过小会增加 IMAP/LLM 压力。建议区间见 `.env.example`（常见 60–300）。 |
| `AUTO_START_POLLING` | false | **进程启动后是否默认开启后台轮询**（`true` 则随进程自动拉信；`false` 须仪表盘「启动轮询」或 `POST /start-auto`） |
| `MAX_EMAILS_PER_CYCLE` | 20 | 每个邮箱在每轮最多拉取 / 处理的未读邮件上限 |
| `MAILBOX_FETCH_MAX_WORKERS` | 8 | **并行连接 IMAP 拉取邮箱数的上限**（实际为 `min(该值, 已启用邮箱数)`；**1** 表示完全顺序拉取）。夹在 **1～64**。多收件箱时可适当调大以缩短单轮拉信阶段耗时；过大可能触发邮服或网络侧并发连接限制。 |
| `EMAIL_PROCESS_MAX_WORKERS` | 4 | **每轮处理来信时的并行线程上限**（夹在 **1～16**；**1** 表示处理阶段完全串行，与旧行为一致）。实际并发为 `min(该值, 本会话批次数)`：系统按 **邮件会话**（`thread_scoped`）分组，**同一会话内**多封未读仍**顺序**处理，**不同会话**可并行以缩短总墙钟时间。过大可能触发 OpenRouter 限流或 SQLite 锁等待。 |

---

## Breaking Changes（与旧版差异）

以下功能已在本版本移除：

| 移除项 | 说明 |
|---|---|
| 主动外呼 / 批量邀请 | `send_outreach_email` 已删除，代码库中无可达外呼发送路径 |
| 外呼批次（campaigns） | `POST /campaigns/draft`、`POST /campaigns/{id}/send` 等路由已移除 |
| 合作工单（leads） | `GET /leads`、`PATCH /leads/{id}/status`、`GET /leads/export` 已移除 |
| 达人 CSV 批量导入 | `POST /creators/import-csv` 已移除 |
| 仪表盘客户表 & `creator_service` | 「来函客户」页及 `GET/POST/PUT/DELETE /creators` 等路由已移除；`app/services/creator_service.py` 已删除；`creators` 表仍由收信流程自动维护 |
| 意图分类（interested / not_interested） | 替换为情绪分析（satisfied / neutral / dissatisfied）+ 语气（cooperative / firm / hostile） |
| LangGraph outbound_graph | 文件已删除，`run_outbound_graph` 不再存在 |
| campaign_service.py | 文件已删除 |

---

## 注意事项

- **多邮箱与对话隔离**：`thread_scope` 将会话键与 **mailbox_id** 组合为 `thread_id`，**来信历史、LLM 上下文、升级记录按会话隔离**，不会在多站点邮箱之间混用同一线程。`creators` 表按客户 **邮箱全局唯一**：同人连系多站时联系人主档共用一条，**不等于**对话内容串台。
- **SMTP/MIME**：`mail_service.py` 中对外回复使用与阿里云邮箱网页投递相近的多部分结构与会话头（`In-Reply-To` / `References` 等），便于与手工网页回信保持一致、降低误判风险；域名 SPF/DKIM 等仍以邮箱服务商与 DNS 配置为准。
- **发信主机与收信主机分离**：例如阿里云企业邮 **IMAP** 为 `imap.qiye.aliyun.com`，**SMTP** 须填 **`smtp.qiye.aliyun.com`**（勿把 IMAP 主机填进 SMTP）。端口常见为 **465 + SMTP SSL**，或 **587 + 关闭 SSL 隐含连接**（`STARTTLS`，与仪表盘勾选一致）。填错易出现「测试/发信失败、收信仍正常」。
- **SQLite 并行写**：数据库连接已启用 **WAL** 与 **busy_timeout**，多会话并行处理时可降低锁冲突。工作目录下可能出现 `-wal` / `-shm` 文件，属正常现象。
- **Windows 与 TLS**：若 `SMTP_SSL` 握手报 `FileNotFoundError` 等证书路径错误，可检查环境变量 `SSL_CERT_FILE` / `SSL_CERT_DIR` 是否指向不存在路径；`imap_tools` 与 `smtplib` 的 TLS 路径不完全相同，可能出现仅 IMAP 通过、SMTP 失败。
- **配置容错**：`PORT`、各超时/轮询/截断等数字型环境变量若填写非数字，将自动回退为内置默认值，避免进程无法启动。
- **纯被动入站**：系统只响应收到的来信，不发送任何主动外呼邮件。
- **外呼代码路径不可达**：`outbound_graph.py`、`campaign_service.py`、`send_outreach_email` 均已从代码库删除。
- **多轮历史与线程绑定**：历史绝不跨线程混用，最多保留最近 `MAX_THREAD_MESSAGES` 条送 LLM。
- **我方回复可信来源**：仅在 SMTP 发送成功后才写入 `thread_messages(role=our)`。
- **轮询与手动检查互斥**：后台轮询与 `POST /check` **共用一把锁**；同一时间只会执行一处 `run_check_cycle`，避免出现双份处理。**尽快响应**：在可接受的资源占用下可把 `POLL_INTERVAL` 调小；极短延迟需邮服侧 **IMAP IDLE / 推送** 等机制，本项目为轮询模式。
- **重复处理保护**：`processed_messages` 表保证同一封邮件不会被处理两次。
- **数据库兼容**：`campaigns`、`outreach_messages`、`tickets` 表仍存在（兼容历史数据），但不再写入新数据。
