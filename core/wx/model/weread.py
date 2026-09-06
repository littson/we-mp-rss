import base64
import html
import json
import re
import threading
import time
from typing import Optional
from urllib.parse import quote, unquote_plus, urlsplit, urlunsplit

import requests

from core.config import cfg
from core.models.feed import Feed
from core.print import print_info, print_warning
from core.wx.base import WxGather
from driver.weread_store import WereadStore


class WereadAuthenticationError(RuntimeError):
    """微信读书 Cookie 缺失或登录状态失效。"""


class WereadResponseError(RuntimeError):
    """微信读书接口返回了无法识别的数据。"""


class MpsWeread(WxGather):
    BASE_URL = "https://weread.qq.com"
    PAGE_SIZE = 20
    SHELF_ADD_BATCH_SIZE = 50
    SESSION_RENEWAL_WINDOW = 300
    SESSION_ERROR_CODES = {-2012, -2010}
    TRACKING_QUERY_PARAMS = {
        "ascene",
        "clicktime",
        "countrycode",
        "devicetype",
        "enterid",
        "exportkey",
        "fontgear",
        "from",
        "isappinstalled",
        "key",
        "lang",
        "nettype",
        "pass_ticket",
        "poc_token",
        "scene",
        "sessionid",
        "subscene",
        "uin",
        "version",
        "wx_header",
    }
    _renewal_lock = threading.Lock()

    def __init__(self, session=None, cookie: Optional[str] = None, store=None):
        super().__init__()
        if session is not None:
            self.session = session
        self.store = store or WereadStore
        stored_cookie = self.store.cookie_header() if cookie is None else ""
        self._managed_session = cookie is None and bool(stored_cookie)
        self.weread_cookie = (
            cookie
            if cookie is not None
            else stored_cookie or cfg.get("weread.cookie", "")
        ) or ""
        self.weread_timeout = int(cfg.get("weread.timeout", 30) or 30)
        self.headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{self.BASE_URL}/web/shelf",
            "User-Agent": self.user_agent,
        }
        if self.weread_cookie:
            self.headers["Cookie"] = self.weread_cookie

    def _set_cookie_header(self, cookie_header):
        self.weread_cookie = cookie_header or ""
        if self.weread_cookie:
            self.headers["Cookie"] = self.weread_cookie
        else:
            self.headers.pop("Cookie", None)

    def _cookie_value(self, name):
        for part in self.weread_cookie.split(";"):
            key, separator, value = part.strip().partition("=")
            if separator and key == name:
                return value
        return None

    def _renew_session(self, request_path):
        with self._renewal_lock:
            if self._managed_session:
                latest_cookie = self.store.cookie_header()
                if latest_cookie and latest_cookie != self.weread_cookie:
                    self._set_cookie_header(latest_cookie)
                    if not self.store.needs_renewal(self.SESSION_RENEWAL_WINDOW):
                        return
            response = self.session.post(
                f"{self.BASE_URL}/web/login/renewal",
                json={
                    "rq": quote(request_path, safe="~()*!.'"),
                    "ql": self._cookie_value("wr_ql") == "1",
                },
                headers=self.headers,
                proxies=self._get_proxies(),
                timeout=(10, self.weread_timeout),
            )
            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise WereadAuthenticationError(
                    "微信读书自动续期失败，请重新扫码授权"
                ) from exc
            if response.status_code >= 400 or payload.get("succ") != 1:
                raise WereadAuthenticationError(
                    "微信读书自动续期失败，请重新扫码授权"
                )
            response_cookies = getattr(response, "cookies", None)
            if self._managed_session:
                self.store.merge(response_cookies)
                renewed_cookie = self.store.cookie_header()
                renewal_persisted = not self.store.needs_renewal(
                    self.SESSION_RENEWAL_WINDOW
                )
            else:
                renewed_cookie = self._merge_cookie_header(response_cookies)
                renewal_persisted = bool(response_cookies)
            if not renewed_cookie or not renewal_persisted:
                raise WereadAuthenticationError(
                    "微信读书自动续期未返回新凭据，请重新扫码授权"
                )
            self._set_cookie_header(renewed_cookie)

    def _merge_cookie_header(self, cookies):
        values = {}
        for part in self.weread_cookie.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name:
                values[name] = value
        for item in self.store._cookie_items(cookies):
            values[item["name"]] = item.get("value", "")
        return "; ".join(f"{name}={value}" for name, value in values.items())

    def _ensure_session(self, request_path):
        if (
            self._managed_session
            and self.store.needs_renewal(self.SESSION_RENEWAL_WINDOW)
        ):
            self._renew_session(request_path)

    @classmethod
    def _is_session_error(cls, payload):
        return (
            isinstance(payload, dict)
            and payload.get("errCode") in cls.SESSION_ERROR_CODES
        )

    def _request(self, path, params=None, expect_json=True):
        if not self.weread_cookie:
            raise WereadAuthenticationError(
                "微信读书未登录，请先扫码授权或设置 WEREAD_COOKIE"
            )
        self._ensure_session(path)
        for attempt in range(2):
            response = self.session.get(
                f"{self.BASE_URL}{path}",
                params=params,
                headers=self.headers,
                proxies=self._get_proxies(),
                timeout=(10, self.weread_timeout),
            )
            payload = None
            if expect_json or response.status_code in (401, 403):
                try:
                    payload = response.json()
                except (ValueError, json.JSONDecodeError):
                    payload = None
            if (
                attempt == 0
                and (response.status_code in (401, 403) or self._is_session_error(payload))
            ):
                self._renew_session(path)
                continue
            if response.status_code in (401, 403):
                raise WereadAuthenticationError(
                    "微信读书登录已失效，请重新扫码授权"
                )
            response.raise_for_status()
            if not expect_json:
                return response.text
            if payload is None:
                raise WereadAuthenticationError(
                    "微信读书接口未返回 JSON，登录状态可能已失效"
                )
            if self._is_session_error(payload):
                raise WereadAuthenticationError(
                    "微信读书自动续期后仍未登录，请重新扫码授权"
                )
            if not isinstance(payload, dict):
                raise WereadResponseError("微信读书接口返回格式错误")
            return payload

    def _post_json(self, path, payload):
        if not self.weread_cookie:
            raise WereadAuthenticationError(
                "微信读书未登录，请先扫码授权或设置 WEREAD_COOKIE"
            )
        self._ensure_session(path)
        for attempt in range(2):
            response = self.session.post(
                f"{self.BASE_URL}{path}",
                json=payload,
                headers=self.headers,
                proxies=self._get_proxies(),
                timeout=(10, self.weread_timeout),
            )
            try:
                result = response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise WereadAuthenticationError(
                    "微信读书接口未返回 JSON，登录状态可能已失效"
                ) from exc
            if (
                attempt == 0
                and (response.status_code in (401, 403) or self._is_session_error(result))
            ):
                self._renew_session(path)
                continue
            if response.status_code in (401, 403):
                raise WereadAuthenticationError(
                    "微信读书登录已失效，请重新扫码授权"
                )
            response.raise_for_status()
            if self._is_session_error(result):
                raise WereadAuthenticationError(
                    "微信读书自动续期后仍未登录，请重新扫码授权"
                )
            if not isinstance(result, dict):
                raise WereadResponseError("微信读书接口返回格式错误")
            return result

    def add_to_shelf(self, book_ids):
        normalized = self._normalize_book_ids(book_ids)
        if not normalized or any(
            not re.fullmatch(r"MP_WXS_[A-Za-z0-9_-]+", item)
            for item in normalized
        ):
            raise ValueError("book_ids 必须是 MP_WXS_ 开头的公众号 ID")
        result = self._post_json("/web/shelf/add", {"bookIds": normalized})
        if result.get("succ") != 1:
            raise WereadResponseError("微信读书加入书架失败")
        return {"succ": 1, "book_ids": normalized}

    @staticmethod
    def _normalize_book_ids(book_ids):
        return list(dict.fromkeys(str(item).strip() for item in book_ids))

    def get_shelf_book_ids(self):
        payload = self._request(
            "/web/shelf/sync",
            {"synckey": 0, "lectureSynckey": 0},
        )
        book_ids = set()
        for item in payload.get("books") or []:
            if isinstance(item, dict) and item.get("bookId"):
                book_ids.add(str(item["bookId"]))
        mp_book = (payload.get("mp") or {}).get("book") or {}
        if isinstance(mp_book, dict) and mp_book.get("bookId"):
            book_ids.add(str(mp_book["bookId"]))
        for archive in payload.get("archive") or []:
            if not isinstance(archive, dict):
                continue
            book_ids.update(str(item) for item in archive.get("bookIds") or [])
        return book_ids

    def add_missing_to_shelf(self, book_ids):
        normalized = self._normalize_book_ids(book_ids)
        if not normalized or any(
            not re.fullmatch(r"MP_WXS_[A-Za-z0-9_-]+", item)
            for item in normalized
        ):
            raise ValueError("book_ids 必须是 MP_WXS_ 开头的公众号 ID")
        existing_shelf = self.get_shelf_book_ids()
        existing = [item for item in normalized if item in existing_shelf]
        missing = [item for item in normalized if item not in existing_shelf]
        for offset in range(0, len(missing), self.SHELF_ADD_BATCH_SIZE):
            self.add_to_shelf(missing[offset:offset + self.SHELF_ADD_BATCH_SIZE])
        return {
            "succ": 1,
            "book_ids": normalized,
            "added_book_ids": missing,
            "existing_book_ids": existing,
            "added_count": len(missing),
            "existing_count": len(existing),
            "total": len(normalized),
        }

    def get_book_info(self, book_id):
        if not re.fullmatch(r"MP_WXS_[A-Za-z0-9_-]+", str(book_id)):
            raise ValueError("book_id 必须是 MP_WXS_ 开头的公众号 ID")
        return self._request("/web/book/info", {"bookId": book_id})

    @staticmethod
    def _profile_value(payload, *keys):
        candidates = [(payload, 0)]
        while candidates:
            candidate, depth = candidates.pop(0)
            if not isinstance(candidate, dict):
                continue
            for key in keys:
                value = candidate.get(key)
                if value not in (None, ""):
                    return value
            if depth < 3:
                candidates.extend(
                    (value, depth + 1)
                    for value in candidate.values()
                    if isinstance(value, dict)
                )
        return ""

    def get_mp_profile(self, book_id):
        """通过微信读书校验公众号，并返回统一的公众号资料。"""
        book_info = self.get_book_info(book_id)
        cover_info = self._request("/web/mp/cover", {"bookId": book_id})
        returned_book_id = self._profile_value(
            book_info, "bookId", "book_id"
        ) or self._profile_value(cover_info, "bookId", "book_id")
        if returned_book_id and str(returned_book_id) != str(book_id):
            raise WereadResponseError("微信读书返回的公众号 ID 不匹配")
        mp_name = self._profile_value(
            cover_info, "mpName", "mp_name", "name", "title"
        ) or self._profile_value(
            book_info, "mpName", "mp_name", "title", "name"
        )
        if not mp_name:
            raise WereadResponseError("微信读书未返回公众号名称")
        return {
            "book_id": str(book_id),
            "mp_name": str(mp_name),
            "mp_cover": str(
                self._profile_value(
                    cover_info,
                    "cover",
                    "coverUrl",
                    "cover_url",
                    "avatar",
                    "logo",
                )
                or self._profile_value(
                    book_info,
                    "cover",
                    "coverUrl",
                    "cover_url",
                    "avatar",
                    "logo",
                )
                or ""
            ),
            "mp_intro": str(
                self._profile_value(
                    cover_info, "intro", "description", "signature"
                )
                or self._profile_value(
                    book_info, "intro", "description", "signature"
                )
                or ""
            ),
        }

    def _get_article_groups(self, book_id, offset):
        try:
            payload = self._request(
                "/web/mp/articles",
                {"bookId": book_id, "offset": offset},
            )
        except requests.HTTPError as exc:
            response = getattr(exc, "response", None)
            try:
                error_payload = response.json() if response is not None else {}
            except (ValueError, json.JSONDecodeError):
                error_payload = {}
            if error_payload.get("errCode") == -2041:
                return self._get_cover_article_groups(book_id, offset)
            raise
        if payload.get("errCode") == -2041:
            return self._get_cover_article_groups(book_id, offset)
        groups = payload.get("reviews")
        if not isinstance(groups, list):
            raise WereadResponseError("文章列表缺少 reviews 字段")
        return groups

    def _get_cover_article_groups(self, book_id, offset):
        if offset:
            return []
        payload = self._request("/web/mp/cover", {"bookId": book_id})
        review_id = self._profile_value(payload, "reviewId", "review_id")
        if not review_id:
            raise WereadResponseError("公众号文章列表不可用，封面也缺少 reviewId")
        return [{
            "createTime": 0,
            "subCount": 1,
            "subReviews": [{"reviewId": str(review_id)}],
        }]

    def _get_review(self, review_id):
        payload = self._request(
            "/web/mp/review/single",
            {"reviewId": review_id},
        )
        review = payload.get("review")
        if not isinstance(review, dict):
            raise WereadResponseError("文章元数据缺少 review 字段")
        return review

    def _get_content(self, review_id):
        html = self._request(
            "/web/mp/content",
            {"reviewId": review_id},
            expect_json=False,
        )
        if not html or "<html" not in html[:1000].lower():
            raise WereadResponseError("文章正文不是有效 HTML")
        from core.wx.content import parse_article_content

        return parse_article_content(html) or self.remove_common_html_elements(html)

    @staticmethod
    def _book_id(faker_id, mps_id):
        if mps_id:
            return mps_id
        if faker_id and faker_id.startswith("MP_WXS_"):
            return faker_id
        try:
            decoded = base64.b64decode(faker_id).decode("utf-8")
        except Exception as exc:
            raise ValueError("无法从公众号 ID 生成微信读书 bookId") from exc
        return f"MP_WXS_{decoded}"

    @staticmethod
    def _iter_reviews(groups):
        for group in groups:
            if not isinstance(group, dict):
                continue
            for item in group.get("subReviews") or []:
                if isinstance(item, dict):
                    yield group, item

    @staticmethod
    def _review_matches_book(review_id, book_id):
        review_id = str(review_id or "")
        book_id = str(book_id or "")
        return not review_id.startswith("MP_WXS_") or review_id.startswith(
            f"{book_id}_"
        )

    @classmethod
    def _normalize_doc_url(cls, url):
        value = html.unescape(str(url or "").strip())
        if not value:
            return ""
        parsed = urlsplit(value)
        if (parsed.hostname or "").lower() != "mp.weixin.qq.com":
            return value
        path = parsed.path.rstrip("/")
        if not (path == "/s" or path.startswith("/s/")):
            return value
        if path.startswith("/s/") and len(path) > len("/s/"):
            query = ""
        else:
            query_parts = []
            for part in parsed.query.split("&"):
                if not part:
                    continue
                key = unquote_plus(part.partition("=")[0]).lower()
                if key not in cls.TRACKING_QUERY_PARAMS:
                    query_parts.append(part)
            query = "&".join(query_parts)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))

    @classmethod
    def _article_url(cls, mp_info):
        doc_url = cls._normalize_doc_url(mp_info.get("doc_url"))
        if doc_url:
            return doc_url

        original_id = html.unescape(
            str(mp_info.get("originalId") or "").strip()
        )
        if re.fullmatch(r"[A-Za-z0-9_~-]+", original_id):
            return f"https://mp.weixin.qq.com/s/{original_id}"
        return ""

    @staticmethod
    def _unix_seconds(*values):
        for value in values:
            try:
                timestamp = int(float(value))
            except (TypeError, ValueError):
                continue
            if timestamp <= 0:
                continue
            if timestamp > 10_000_000_000:
                timestamp //= 1000
            return timestamp
        return 0

    def _article_data(
        self,
        review,
        review_id,
        mps_id,
        gather_content,
        fallback_timestamp=0,
    ):
        mp_info = review.get("mpInfo") or {}
        timestamp = self._unix_seconds(
            mp_info.get("time"),
            mp_info.get("publishTime"),
            mp_info.get("publish_time"),
            review.get("createTime"),
            fallback_timestamp,
        )
        original_id = mp_info.get("originalId") or ""
        content = self._get_content(review_id) if gather_content else ""
        return {
            "id": str(review_id),
            "mp_id": mps_id,
            "title": mp_info.get("title") or "",
            "link": self._article_url(mp_info),
            "cover": mp_info.get("pic_url") or "",
            "digest": mp_info.get("content") or "",
            "content": content,
            "update_time": timestamp,
            "create_time": timestamp,
            "type": review.get("type", 0),
            "show_type": 0,
            "copyright_stat": 0,
            "is_deleted": False,
            "publish_info": {
                "source": "weread",
                "review_id": review_id,
                "original_id": original_id,
                "mp_name": mp_info.get("mp_name") or "",
                "read_num": mp_info.get("readNum", 0),
                "like_num": mp_info.get("likeNum", 0),
                "pay_type": mp_info.get("payType", 0),
            },
        }

    def get_Articles(
        self,
        faker_id: str = "",
        Mps_id: str = "",
        Mps_title="",
        CallBack=None,
        start_page: int = 0,
        MaxPage: int = 1,
        interval=10,
        Gather_Content=False,
        Item_Over_CallBack=None,
        Over_CallBack=None,
    ):
        self.articles = []
        self.start_time = time.time()
        gather_content = self.Gather_Content
        book_id = self._book_id(faker_id, Mps_id)
        print_info(
            f"微信读书模式,是否采集[{Mps_title}]内容：{gather_content}"
        )
        self.update_mps(Mps_id, Feed(sync_time=int(time.time())))
        latest_publish_time = 0
        seen = set()
        try:
            groups = self._get_article_groups(book_id, 0)
            for group, item in self._iter_reviews(groups):
                review_id = item.get("reviewId")
                if not review_id or review_id in seen:
                    continue
                seen.add(review_id)
                if not self._review_matches_book(review_id, book_id):
                    print_warning(
                        f"忽略其他公众号文章: {review_id}，当前公众号: {book_id}"
                    )
                    continue
                review = item.get("review")
                if not isinstance(review, dict) or not review.get("mpInfo"):
                    review = self._get_review(review_id)
                data = self._article_data(
                    review,
                    review_id,
                    Mps_id,
                    gather_content,
                    fallback_timestamp=group.get("createTime") or 0,
                )
                latest_publish_time = max(
                    latest_publish_time,
                    int(data.get("update_time") or 0),
                )
                if CallBack is not None:
                    self.FillBack(
                        CallBack=CallBack,
                        data=data,
                        Ext_Data={"mp_title": Mps_title, "mp_id": Mps_id},
                    )
            if latest_publish_time:
                self.update_mps(
                    Mps_id,
                    Feed(update_time=latest_publish_time, status=1),
                )
            return self.articles
        finally:
            if Item_Over_CallBack is not None:
                Item_Over_CallBack({"mps_id": Mps_id, "mps_title": Mps_title})
            self.Over(CallBack=Over_CallBack)
