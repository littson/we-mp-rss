import json
import os
import time

from core.config import cfg
from core.file import FileCrypto
from core.redis_client import redis_client


class WereadSessionStore:
    key_file = "data/weread_cookies.lic"
    redis_key = "werss:weread:cookies"

    def __init__(self, key_file=None, use_redis=True):
        self.key_file = key_file or self.key_file
        self.use_redis = use_redis
        self.store = FileCrypto(cfg.get("safe.lic_key", "store.csol.store.werss"))

    def save(self, cookies):
        items = [
            dict(item)
            for item in (cookies or [])
            if "weread.qq.com" in str(item.get("domain", ""))
        ]
        text = json.dumps(items, ensure_ascii=False)
        if self.use_redis and redis_client.is_connected:
            try:
                redis_client._client.set(self.redis_key, text)
            except Exception:
                pass
        directory = os.path.dirname(self.key_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.store.encrypt_to_file(self.key_file, text.encode("utf-8"))
        try:
            os.chmod(self.key_file, 0o600)
        except OSError:
            pass

    def load(self):
        return self._active_cookies(self.load_all())

    def load_all(self):
        items = self._load_from_redis()
        if items is None:
            items = self._load_from_file()
        return [item for item in (items or []) if isinstance(item, dict)]

    def merge(self, cookies):
        merged = {
            self._cookie_key(item): dict(item)
            for item in self.load_all()
            if item.get("name")
        }
        for item in self._cookie_items(cookies):
            key = self._cookie_key(item)
            current = merged.get(key, {})
            current.update(item)
            merged[key] = current
        self.save(list(merged.values()))
        return self.load()

    def needs_renewal(self, within_seconds=300):
        cookies = self.load_all()
        now = time.time()
        has_refresh_cookie = any(
            item.get("name") == "wr_rt" and self._is_active(item, now)
            for item in cookies
        )
        if not has_refresh_cookie:
            return False
        session_cookies = [
            item for item in cookies if item.get("name") == "wr_skey"
        ]
        if not session_cookies:
            return True
        return not any(
            (expires := self._expires_at(item)) is None
            or expires > now + within_seconds
            for item in session_cookies
        )

    def cookie_header(self):
        return "; ".join(
            f"{item['name']}={item['value']}"
            for item in self.load()
            if item.get("name") and item.get("value") is not None
        )

    def has_auth_cookie(self):
        names = {
            item.get("name")
            for item in self.load()
            if item.get("value")
        }
        return {"wr_localvid", "wr_ql"}.issubset(names)

    def clear(self):
        if self.use_redis and redis_client.is_connected:
            try:
                redis_client._client.delete(self.redis_key)
            except Exception:
                pass
        try:
            os.remove(self.key_file)
        except FileNotFoundError:
            pass

    def _load_from_redis(self):
        if not self.use_redis or not redis_client.is_connected:
            return None
        try:
            text = redis_client._client.get(self.redis_key)
            return json.loads(text) if text else None
        except Exception:
            return None

    def _load_from_file(self):
        try:
            text = self.store.decrypt_from_file(self.key_file).decode("utf-8")
            return json.loads(text)
        except Exception:
            return []

    @staticmethod
    def _active_cookies(cookies):
        now = time.time()
        return [
            item
            for item in cookies
            if isinstance(item, dict)
            and item.get("name")
            and WereadSessionStore._is_active(item, now)
        ]

    @staticmethod
    def _expires_at(cookie):
        expires = cookie.get("expires")
        if expires in (None, ""):
            return None
        try:
            value = float(expires)
        except (TypeError, ValueError):
            return None
        return None if value < 0 else value

    @classmethod
    def _is_active(cls, cookie, now=None):
        expires = cls._expires_at(cookie)
        return expires is None or expires > (now or time.time())

    @staticmethod
    def _cookie_key(cookie):
        return (
            str(cookie.get("name", "")),
            str(cookie.get("domain", ".weread.qq.com")),
            str(cookie.get("path", "/")),
        )

    @staticmethod
    def _cookie_items(cookies):
        for cookie in cookies or []:
            if isinstance(cookie, dict):
                item = dict(cookie)
            else:
                item = {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain or ".weread.qq.com",
                    "path": cookie.path or "/",
                    "expires": cookie.expires if cookie.expires is not None else -1,
                    "httpOnly": bool(cookie._rest.get("HttpOnly")),
                    "secure": bool(cookie.secure),
                }
            if item.get("name"):
                yield item


WereadStore = WereadSessionStore()
