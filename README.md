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
   │   若升级且配置 AUTO_REPLY_ON_ESCALATION，在回复末尾追加「已转交」说明
       ↓
   SMTP send_reply → 用户收到回信
       ↓（若需升级）
   向产品负责人发送内部升级通知邮件
   记录 escalation_events
```

---

## 功能概览

| 功能 | 说明 |
|---|---|
| **情绪与语气分析** | 每封来信自动分析 sentiment（满意/中性/不满意）和 tone（配合/强硬/敌对） |
| **安抚回复生成** | 不满意/强硬/敌对时优先发送道歉+共情+说明类回复 |
| **普通客服回复** | 满意/中性+配合时直接回答问题，简洁专业 |
| **升级人工处理** | 满足条件时向产品 owner_email 发送内部通知邮件，含摘要与原文节选 |
| **升级冷却保护** | 同线程升级邮件在冷却时间内不重复发送，避免刷屏 |
| **产品匹配** | 线程已绑定产品优先使用，否则用关键词匹配产品库兜底 |
| **多轮对话记忆** | 按邮件线程（Thread）隔离存储完整收发历史，最多送 LLM N 条 |
| **Web 仪表盘** | 会话线程、升级记录、情绪流水、产品库（含负责人）、联系人、日志 |

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

# 4. 打开仪表盘
# http://localhost:8000/dashboard
```

---

## 项目结构

```
.
├── app/
│   ├── agent.py              # 来信处理核心：收信→分析情绪→升级判断→发回复→写升级记录
│   ├── config.py             # 环境变量统一读取（含客服配置项）
│   ├── database.py           # SQLite 持久化层
│   ├── llm_service.py        # LLM 调用：情绪分析 + 回复生成 + 升级摘要
│   ├── mail_service.py       # IMAP 收件 + SMTP 发件（用户回复 & 内部升级通知）
│   ├── main.py               # FastAPI 入口 + 仪表盘路由
│   ├── graphs/
│   │   └── inbound_graph.py  # LangGraph 客服状态机（5 节点）
│   ├── services/
│   │   ├── creator_service.py   # 联系人管理
│   │   ├── lead_service.py      # 情绪流水查询（意图识别结果）
│   │   └── product_service.py   # 产品库管理（含 owner 字段）
│   └── web/
│       └── dashboard.html    # 单页 Web 仪表盘
├── data/
│   └── products.json         # 本地产品库（初始种子数据，含 owner_name/owner_email）
├── .env                      # 实际配置（不提交 Git）
├── .env.example              # 配置模板
├── kol_agent.db              # SQLite 数据库（自动创建）
└── requirements.txt
```

---

## 数据库表结构

系统使用 SQLite，共 8 张表：

| 表名 | 职责 |
|---|---|
| `creators` | 联系人主档：邮箱、姓名、语言、备注 |
| `products` | 产品库：名称、关键词、卖点、**负责人 owner_name / owner_email** |
| `kol_threads` | 邮件线程状态：情绪标签、最后处理消息、绑定产品 |
| `thread_messages` | 多轮对话历史：每封来信和我方回复 |
| `intent_results` | 每封来信的情绪识别结果（cs_sentiment / cs_tone / escalated） |
| `escalation_events` | 升级事件记录：通知发送至、升级原因、时间戳 |
| `processed_messages` | 已处理邮件去重记录 |
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

### `products` — 产品负责人字段（1A 维护入口）

| 字段 | 说明 |
|---|---|
| `owner_name` | 产品负责人姓名 |
| `owner_email` | 升级通知首选收件邮箱 |
| `fallback_owner_email` | owner_email 为空时的备用邮箱 |

---

## 产品绑定与负责人（1A）

**规则**：产品的 `owner_email` 是升级通知的唯一收件人来源，仅通过以下方式维护：

1. 编辑 `data/products.json` 后重新初始化数据库（首次空库自动 seed）
2. 通过仪表盘「产品库」Tab 的表单更新（`POST /products`）
3. 直接修改 SQLite 数据库中 `products` 表的 `owner_email` 字段

升级收件人优先级：`product.owner_email` → `product.fallback_owner_email` → `DEFAULT_SUPPORT_OWNER_EMAIL`

**不提供**：线程级别的负责人切换 UI、`/products/{id}/owner` 独立 API、邮件密语绑定。

---

## 内部升级邮件说明

升级邮件主题格式：`[客服升级] {联系人姓名} — {产品名称}`

邮件内容包含：
- 线程 ID
- 联系人姓名与邮箱
- 绑定产品名称
- 优先级建议（high / medium）
- LLM 生成的 ≤5 行摘要
- 升级触发原因

**升级冷却**：同一线程在 `ESCALATION_EMAIL_COOLDOWN_MINUTES` 分钟内不重复发送升级通知（防止刷屏）。

---

## 仪表盘说明

访问 `http://localhost:8000/dashboard`

| 标签页 | 内容 |
|---|---|
| **会话/线程** | 线程列表（联系人、情绪标签、最后更新）+ 最近情绪识别预览 |
| **升级记录** | 所有升级事件：联系人、产品、原因、通知发送至 |
| **情绪流水** | 每封来信的 sentiment / tone / 是否升级 |
| **产品库** | 产品增删改，含 owner_name / owner_email 维护（1A） |
| **联系人** | 联系人列表，由系统来信时自动入库 |
| **运行日志** | 最近 80 条运行日志，每 6 秒自动刷新 |

---

## API 端点

| 端点 | 说明 |
|---|---|
| `GET /dashboard` | Web 仪表盘 |
| `POST /check` | 立即触发一轮来信检查 |
| `POST /start-auto` | 启动后台定时轮询 |
| `POST /stop-auto` | 停止后台轮询 |
| `GET /status` | 服务状态与汇总数据（contacts / products / intents / escalations） |
| `GET /creators` | 联系人列表 |
| `POST /creators` | 新增联系人 |
| `PUT /creators/{id}` | 更新联系人信息 |
| `DELETE /creators/{id}` | 删除联系人 |
| `GET /products` | 产品列表（含 owner 字段） |
| `POST /products` | 新增/更新产品（唯一 owner 维护入口） |
| `DELETE /products/{id}` | 删除产品 |
| `GET /intents` | 情绪识别结果列表（含 cs_sentiment / cs_tone / escalated） |
| `DELETE /intents` | 清空情绪识别记录 |
| `GET /escalations` | 升级事件列表 |
| `GET /kols` | 所有线程概览 |
| `GET /thread/{thread_id}` | 线程完整对话历史 |
| `DELETE /thread/{thread_id}` | 删除线程数据 |
| `DELETE /threads` | 清空全部线程数据 |
| `GET /logs` | 最近运行日志 |
| `GET /emails` | 预览当前未读邮件（不触发处理） |

---

## 配置项速查

| 变量 | 默认值 | 说明 |
|---|---|---|
| `EMAIL_ADDRESS` | — | 阿里企业邮箱地址 |
| `EMAIL_PASSWORD` | — | 邮箱密码 |
| `LLM_API_KEY` | — | LLM 服务 API Key |
| `LLM_BASE_URL` | 通义千问 | LLM 接入地址（OpenAI Chat Completions 格式） |
| `LLM_MODEL` | qwen-plus | 模型名称 |
| `BRAND_NAME` | Our Brand | 品牌名（注入 LLM prompt） |
| `BRAND_SIGNATURE` | Support Team | 邮件署名 |
| `DEFAULT_SUPPORT_OWNER_EMAIL` | — | **产品无 owner 时的升级通知收件人** |
| `DEFAULT_SUPPORT_OWNER_NAME` | Support Owner | 全局默认负责人姓名 |
| `AUTO_REPLY_ON_ESCALATION` | true | 升级时是否在用户回信末尾追加「已转交」说明 |
| `ALLOW_COMPENSATION_PROMISES` | false | 是否允许 LLM 在回复中承诺具体赔偿 |
| `REPEAT_DISSATISFACTION_HOURS` | 24 | 判断「二次不满意强制升级」的时间窗（小时） |
| `ESCALATION_EMAIL_COOLDOWN_MINUTES` | 60 | 同线程升级邮件冷却时间（分钟） |
| `DB_FILE` | kol_agent.db | SQLite 数据库文件路径 |
| `PRODUCTS_PATH` | data/products.json | 产品库 JSON 路径 |
| `MAX_THREAD_MESSAGES` | 10 | 每线程送 LLM 的最大历史条数 |
| `BODY_EXCERPT_LENGTH` | 600 | 邮件正文入库截断字符数 |
| `POLL_INTERVAL` | 120 | 轮询间隔（秒） |
| `MAX_EMAILS_PER_CYCLE` | 20 | 每轮最多处理邮件数 |

---

## Breaking Changes（与旧版差异）

以下功能已在本版本移除：

| 移除项 | 说明 |
|---|---|
| 主动外呼 / 批量邀请 | `send_outreach_email` 已删除，代码库中无可达外呼发送路径 |
| 外呼批次（campaigns） | `POST /campaigns/draft`、`POST /campaigns/{id}/send` 等路由已移除 |
| 合作工单（leads） | `GET /leads`、`PATCH /leads/{id}/status`、`GET /leads/export` 已移除 |
| 达人 CSV 批量导入 | `POST /creators/import-csv` 已移除 |
| 意图分类（interested / not_interested） | 替换为情绪分析（satisfied / neutral / dissatisfied）+ 语气（cooperative / firm / hostile） |
| LangGraph outbound_graph | 文件已删除，`run_outbound_graph` 不再存在 |
| campaign_service.py | 文件已删除 |

---

## 注意事项

- **纯被动入站**：系统只响应收到的来信，不发送任何主动外呼邮件。
- **外呼代码路径不可达**：`outbound_graph.py`、`campaign_service.py`、`send_outreach_email` 均已从代码库删除。
- **多轮历史与线程绑定**：历史绝不跨线程混用，最多保留最近 `MAX_THREAD_MESSAGES` 条送 LLM。
- **我方回复可信来源**：仅在 SMTP 发送成功后才写入 `thread_messages(role=our)`。
- **重复处理保护**：`processed_messages` 表保证同一封邮件不会被处理两次。
- **数据库兼容**：`campaigns`、`outreach_messages`、`tickets` 表仍存在（兼容历史数据），但不再写入新数据。
