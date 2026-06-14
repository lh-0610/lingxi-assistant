"""Web 多用户:注册/登录 + token 鉴权 + 每用户数据目录。

只服务于 web/(桌面端单用户,不用)。
- 密码用 pbkdf2-hmac-sha256 加盐哈希存盘,绝不存明文。
- token 随机串、持久化(web_tokens.json),重启不掉线;支持一个用户多 token(多设备)。
- 注册表存在服务器数据根 APP_DIR 下;每个用户的对话/记忆隔离在 APP_DIR/users/<用户名>/。
  配合 src/paths.py 的 set_data_dir():worker/请求线程切到该目录,memory/projects/
  long_term_memory/role_config 全部落到各自子目录,实现数据隔离。
"""
import os
import re
import json
import hashlib
import secrets
import threading
from datetime import datetime

_PBKDF2_ROUNDS = 200_000
# 用户名限定中英文/数字/下划线/连字符 → 直接当目录名也安全(无路径分隔符/点)
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_一-龥-]{1,32}$")


class UserStore:
    """用户注册表 + token 表 + 每用户数据目录解析。线程安全。"""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.users_file = os.path.join(base_dir, "web_users.json")
        self.tokens_file = os.path.join(base_dir, "web_tokens.json")
        self.users_root = os.path.join(base_dir, "users")
        self._lock = threading.RLock()
        os.makedirs(self.users_root, exist_ok=True)

    # ── 持久化(原子写)──
    def _read(self, path: str) -> dict:
        try:
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _write(self, path: str, data: dict) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _hash(self, password: str, salt: str) -> str:
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ROUNDS
        ).hex()

    # ── 用户名 / 数据目录 ──
    @staticmethod
    def valid_username(username: str) -> bool:
        return bool(_USERNAME_RE.match(username or ""))

    def data_dir_for(self, username: str) -> str:
        """该用户的数据根(传给 paths.set_data_dir)。"""
        return os.path.join(self.users_root, username)

    # ── 注册 / 登录 / token ──
    def register(self, username: str, password: str):
        """成功返回 (token, None);失败返回 (None, 错误文案)。"""
        username = (username or "").strip()
        password = password or ""
        if not self.valid_username(username):
            return None, "用户名只能含中英文/数字/下划线/连字符,长度 1-32"
        if len(password) < 4:
            return None, "密码至少 4 位"
        with self._lock:
            users = self._read(self.users_file)
            if username in users:
                return None, "用户名已存在"
            salt = secrets.token_hex(16)
            users[username] = {
                "salt": salt,
                "hash": self._hash(password, salt),
                "created": datetime.now().isoformat(),
            }
            self._write(self.users_file, users)
        os.makedirs(self.data_dir_for(username), exist_ok=True)
        return self.issue_token(username), None

    def login(self, username: str, password: str):
        username = (username or "").strip()
        with self._lock:
            users = self._read(self.users_file)
            u = users.get(username)
            # 用户不存在 / 密码错误统一文案,避免用户名枚举
            if not u or not secrets.compare_digest(
                self._hash(password or "", u["salt"]), u["hash"]
            ):
                return None, "用户名或密码错误"
        return self.issue_token(username), None

    def issue_token(self, username: str) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            tokens = self._read(self.tokens_file)
            tokens[token] = username
            self._write(self.tokens_file, tokens)
        return token

    def user_for_token(self, token: str):
        if not token:
            return None
        with self._lock:
            return self._read(self.tokens_file).get(token)

    def revoke(self, token: str) -> None:
        with self._lock:
            tokens = self._read(self.tokens_file)
            if tokens.pop(token, None) is not None:
                self._write(self.tokens_file, tokens)
