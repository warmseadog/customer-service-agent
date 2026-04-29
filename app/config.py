from pathlib import Path

from dotenv import dotenv_values

# 项目根目录（与启动时 cwd 无关，用于解析相对路径下的 DB/数据文件）
_ROOT = Path(__file__).resolve().parent.parent

# 直接用 dotenv_values 解析 .env，不依赖 os.environ，彻底规避系统环境变量缓存问题
_env_path = _ROOT / ".env"
_env = dotenv_values(_env_path)


def _get(key: str, default: str = "") -> str:
    return _env.get(key) or default


def _int(key: str, default: int) -> int:
    """读取整数配置；.env 填错时回退默认值，避免导入阶段崩溃。"""
    raw = _get(key, str(default))
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _resolved_data_path(key: str, default_relative: str) -> str:
    """相对路径一律相对项目根目录解析；绝对路径保持不变。"""
    raw = (_get(key, default_relative) or default_relative).strip()
    p = Path(raw)
    if p.is_absolute():
        return str(p)
    return str((_ROOT / p).resolve())


class Config:
    # Alibaba Enterprise Mail
    EMAIL_ADDRESS: str = _get("EMAIL_ADDRESS")
    EMAIL_PASSWORD: str = _get("EMAIL_PASSWORD")
    IMAP_HOST: str = _get("IMAP_HOST", "imap.qiye.aliyun.com")
    IMAP_PORT: int = _int("IMAP_PORT", 993)
    SMTP_HOST: str = _get("SMTP_HOST", "smtp.qiye.aliyun.com")
    SMTP_PORT: int = _int("SMTP_PORT", 465)

    # LLM（默认 OpenRouter + Gemini；换供应商改 LLM_BASE_URL / LLM_MODEL）
    LLM_API_KEY: str = _get("LLM_API_KEY")
    LLM_BASE_URL: str = _get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    LLM_MODEL: str = _get("LLM_MODEL", "google/gemini-3.1-pro-preview")
    LLM_TIMEOUT: int = _int("LLM_TIMEOUT", 60)
    # OpenRouter 可选自愿头（映射 HTTP-Referer、X-OpenRouter-Title）：https://openrouter.ai/docs
    LLM_HTTP_REFERER: str = _get("LLM_HTTP_REFERER")
    LLM_APP_TITLE: str = _get("LLM_APP_TITLE")

    # Brand
    BRAND_NAME: str = _get("BRAND_NAME", "Our Brand")
    BRAND_SIGNATURE: str = _get("BRAND_SIGNATURE", "The Partnership Team")
    # 发件人显示名（会出现在收件人的 From 字段，真实人名比纯邮箱地址可信度更高）
    SENDER_DISPLAY_NAME: str = _get("SENDER_DISPLAY_NAME", "Support Team")
    # True：回复主题使用阿里邮箱网页同款「回复：」前缀（MIME 画像更接近手动网页回信）
    MAIL_REPLY_SUBJECT_WEB_STYLE: bool = _get("MAIL_REPLY_SUBJECT_WEB_STYLE", "true").lower() not in (
        "false",
        "0",
        "no",
    )

    # Database（相对路径相对项目根目录）
    DB_FILE: str = _resolved_data_path("DB_FILE", "kol_agent.db")

    # Thread memory
    MAX_THREAD_MESSAGES: int = _int("MAX_THREAD_MESSAGES", 10)
    BODY_EXCERPT_LENGTH: int = _int("BODY_EXCERPT_LENGTH", 600)

    # Products（相对路径相对项目根目录）
    PRODUCTS_PATH: str = _resolved_data_path("PRODUCTS_PATH", "data/products.json")
    SUPPORT_STAFF_PATH: str = _resolved_data_path("SUPPORT_STAFF_PATH", "data/support_staff.json")

    # Customer Service — escalation & reply
    DEFAULT_SUPPORT_OWNER_EMAIL: str = _get("DEFAULT_SUPPORT_OWNER_EMAIL", "")
    DEFAULT_SUPPORT_OWNER_NAME: str = _get("DEFAULT_SUPPORT_OWNER_NAME", "Support Owner")
    # 保留供后续接入：是否在升级场景下统一切换对外话术（当前由安抚流 after_sales_notified 与提示词控制，见 README）
    AUTO_REPLY_ON_ESCALATION: bool = _get("AUTO_REPLY_ON_ESCALATION", "true").lower() not in (
        "false",
        "0",
        "no",
    )
    ALLOW_COMPENSATION_PROMISES: bool = _get("ALLOW_COMPENSATION_PROMISES", "false").lower() in (
        "true",
        "1",
        "yes",
    )
    REPEAT_DISSATISFACTION_HOURS: int = _int("REPEAT_DISSATISFACTION_HOURS", 24)
    ESCALATION_EMAIL_COOLDOWN_MINUTES: int = _int("ESCALATION_EMAIL_COOLDOWN_MINUTES", 60)
    # 不满/安抚类：首次对外承诺「已联系售后」时，是否忽略同线程升级邮件冷却，确保每封都能发出内部通知
    CALM_BYPASS_ESCALATION_COOLDOWN: bool = _get("CALM_BYPASS_ESCALATION_COOLDOWN", "true").lower() not in (
        "false",
        "0",
        "no",
    )

    # Server
    HOST: str = _get("HOST", "0.0.0.0")
    PORT: int = _int("PORT", 8000)
    POLL_INTERVAL: int = _int("POLL_INTERVAL", 120)
    MAX_EMAILS_PER_CYCLE: int = _int("MAX_EMAILS_PER_CYCLE", 20)
    # 每轮检查时并行连接 IMAP 的上限（1=顺序拉取）；多邮箱时拉大以缩短单轮耗时，过大可能触发邮服/NAT 限连
    _mw = _int("MAILBOX_FETCH_MAX_WORKERS", 8)
    MAILBOX_FETCH_MAX_WORKERS: int = max(1, min(64, _mw))
    # 本条回信之前线程内我方回信数 ≥ 该阈值时，安抚对外采用 empathy_pure（不重复售后时间线套话）；范围 1–32
    _emp = _int("CALM_EMPATHY_ONLY_MIN_PRIOR_OUTBOUND", 3)
    CALM_EMPATHY_ONLY_MIN_PRIOR_OUTBOUND: int = max(1, min(32, _emp))
    # 进程启动后是否立即开启定时轮询（需手动 POST /stop-auto 才会停）
    AUTO_START_POLLING: bool = _get("AUTO_START_POLLING", "true").lower() not in (
        "false",
        "0",
        "no",
    )


config = Config()
