"""线程 ID 作用域：mailbox_id + RFC 线索，避免多收件箱键冲突。"""

SEP = "\x1e"  # ASCII RS，极少出现在 Message-ID / References 首 token 中


def scope_thread_id(mailbox_id: int, raw_key: str) -> str:
    return f"{mailbox_id}{SEP}{raw_key}"


def parse_scoped_thread_id(thread_id: str) -> tuple[int, str]:
    if SEP not in thread_id:
        return 1, thread_id
    a, _, b = thread_id.partition(SEP)
    try:
        return int(a), b
    except ValueError:
        return 1, thread_id


def scope_message_stub(mailbox_id: int, raw: str) -> str:
    """用于去重键、thread_messages.message_id 等与邮箱作用域绑定的存储。"""
    return f"{mailbox_id}{SEP}{raw}"
