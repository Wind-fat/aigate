"""供应商密钥的加密存储。

为什么要有这一层：只认环境变量（`AIGATE_DASHSCOPE_API_KEY`）时，一个内部团队的运维
**没有任何办法**把刚申请到的 key 填进去——他得改 `.env`、重启进程，还要在聊天里
把 key 发给别人让他代填。控制台上那个"未配置密钥、测试失败 0ms"的红条，
本质上说的就是这件事。

所以密钥可以落库，但只以密文形式：

- 主密钥来自 `AIGATE_SECRET_KEY`（或用 `AIGATE_SESSION_SECRET` 派生，启动时告警）。
- 用 Fernet（AES-128-CBC + HMAC-SHA256），每条密文带随机 IV，同一把 key 存两次密文不同。
- 数据库被拖走、备份被拷走，**单独一份文件解不开**；要还原必须同时拿到进程环境里的主密钥。
- 明文只在两个地方出现：写入的那一次请求体、以及发给上游的那一个 HTTP 头。
  任何读接口都不返回明文，只返回末四位（`sk-…a1b2`）。

这不是"生产级 KMS"：没有 HSM、没有自动轮换、没有按租户分密钥。它解决的是
"内部团队能自己把 key 填进去，且盘上的副本不足以直接使用"这两个具体问题。
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken


class SecretError(RuntimeError):
    """密文解不开：主密钥换过，或者密文被篡改。"""


def _fernet(secret: str) -> Fernet:
    # Fernet 要 32 字节 urlsafe base64；把任意长度的密钥哈希成固定长度。
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


@dataclass
class SecretBox:
    secret: str

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        return _fernet(self.secret).encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext:
            return ""
        try:
            return _fernet(self.secret).decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:  # 主密钥换了 / 密文损坏
            raise SecretError(
                "cannot decrypt this provider secret; AIGATE_SECRET_KEY changed? "
                "re-enter the key in the console"
            ) from exc


def hint(plaintext: str) -> str:
    """末四位：足够认出来是哪把 key，不足以使用。"""
    if not plaintext:
        return ""
    tail = plaintext[-4:] if len(plaintext) > 4 else plaintext
    return f"…{tail}"


def looks_like_key(value: str) -> bool:
    """松校验：只挡明显的手滑（空、带空格、太短）。

    各家的前缀都不一样（sk- / sk-ant- / 无前缀的自建服务），在这里做严格正则
    只会让自建 vLLM 填不进去——那是一个很常见的"系统坏了"的误报。
    """
    v = value.strip()
    return len(v) >= 8 and " " not in v and "\n" not in v
