import asyncio
import base64

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from apis.mps import (
    ArticleSubscriptionRequest,
    search_mp,
    subscribe_mp_by_article,
)
from core.models.base import Base
from core.models.feed import Feed
from core.wx.model.weread import MpsWeread, WereadResponseError
from driver.wxarticle import WXArticleFetcher
from tests.test_weread_model import FakeResponse, _model


class FakeLocator:
    def __init__(self, value=""):
        self.value = value

    async def get_attribute(self, name, timeout=None):
        return self.value


class FakePage:
    def __init__(self, biz, mp_name="测试公众号"):
        self.biz = biz
        self.mp_name = mp_name

    async def evaluate(self, script):
        return {
            "biz": self.biz,
            "nickname": self.mp_name,
            "userName": "gh_test",
            "alias": "test",
        }

    def locator(self, selector):
        if selector == 'meta[property="og:article:author"]':
            return FakeLocator(self.mp_name)
        return FakeLocator("https://example.com/avatar.jpg")


class FakeController:
    page = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.page = self.__class__.page

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def open_url(self, url, timeout=None):
        return True


def _encoded_numeric_id(value="3581557283"):
    return base64.b64encode(value.encode()).decode()


def test_article_identity_uses_runtime_publisher_biz(monkeypatch):
    correct_biz = _encoded_numeric_id()
    FakeController.page = FakePage(correct_biz, "正确公众号")
    monkeypatch.setattr("driver.wxarticle.PlaywrightController", FakeController)

    result = asyncio.run(WXArticleFetcher().get_mp_identity(
        "https://mp.weixin.qq.com/s/example?__biz=MzI4NjE5MDIzNg=="
    ))

    assert result == {
        "mp_info": {
            "mp_name": "正确公众号",
            "logo": "https://example.com/avatar.jpg",
            "biz": correct_biz,
        },
        "mp_id": "MP_WXS_3581557283",
    }


def test_article_identity_rejects_non_wechat_host(monkeypatch):
    monkeypatch.setattr("driver.wxarticle.PlaywrightController", FakeController)

    with pytest.raises(ValueError, match="有效的微信公众号文章链接"):
        asyncio.run(WXArticleFetcher().get_mp_identity(
            "https://example.com/s/example"
        ))


def test_article_identity_rejects_non_numeric_biz(monkeypatch):
    FakeController.page = FakePage(base64.b64encode(b"not-numeric").decode())
    monkeypatch.setattr("driver.wxarticle.PlaywrightController", FakeController)

    with pytest.raises(ValueError, match="唯一 ID 格式无效"):
        asyncio.run(WXArticleFetcher().get_mp_identity(
            "https://mp.weixin.qq.com/s/example"
        ))


def test_weread_profile_merges_book_and_cover_data():
    model = _model([
        FakeResponse(payload={
            "bookId": "MP_WXS_3581557283",
            "title": "测试公众号",
            "intro": "测试简介",
        }),
        FakeResponse(payload={
            "book": {
                "bookId": "MP_WXS_3581557283",
                "name": "测试公众号",
                "cover": "https://example.com/cover.jpg",
            }
        }),
    ])

    assert model.get_mp_profile("MP_WXS_3581557283") == {
        "book_id": "MP_WXS_3581557283",
        "mp_name": "测试公众号",
        "mp_cover": "https://example.com/cover.jpg",
        "mp_intro": "测试简介",
    }


def test_weread_profile_rejects_mismatched_book_id():
    model = _model([
        FakeResponse(payload={
            "bookId": "MP_WXS_other",
            "title": "错误公众号",
        }),
        FakeResponse(payload={"name": "错误公众号"}),
    ])

    with pytest.raises(WereadResponseError, match="ID 不匹配"):
        model.get_mp_profile("MP_WXS_3581557283")


class FakeWeread:
    added_count = 1
    fail_shelf = False

    def get_mp_profile(self, book_id):
        return {
            "book_id": book_id,
            "mp_name": "测试公众号",
            "mp_cover": "https://example.com/cover.jpg",
            "mp_intro": "测试简介",
        }

    def add_missing_to_shelf(self, book_ids):
        if self.fail_shelf:
            raise RuntimeError("加入书架失败")
        return {
            "added_count": self.added_count,
            "existing_count": int(not self.added_count),
        }


class FakeIdentityFetcher:
    async def get_mp_identity(self, url):
        biz = _encoded_numeric_id()
        return {
            "mp_id": "MP_WXS_3581557283",
            "mp_info": {
                "mp_name": "文章页名称",
                "logo": "https://example.com/article-avatar.jpg",
                "biz": biz,
            },
        }


def _request():
    return Request({
        "type": "http",
        "method": "POST",
        "scheme": "https",
        "server": ("rss.example.com", 443),
        "path": "/api/v1/wx/mps/by_article/subscribe",
        "root_path": "",
        "query_string": b"",
        "headers": [],
    })


def test_subscribe_by_article_returns_absolute_rss_and_is_idempotent(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr("apis.mps.DB.get_session", sessions)
    monkeypatch.setattr("apis.mps.WXArticleFetcher", FakeIdentityFetcher)
    monkeypatch.setattr("apis.mps.MpsWeread", FakeWeread)
    monkeypatch.setattr("apis.mps.save_avatar_locally", lambda value: value)
    monkeypatch.setattr("apis.mps._reload_scheduler_jobs", lambda: None)
    queued = []
    monkeypatch.setattr("apis.mps._enqueue_initial_fetch", queued.append)
    FakeWeread.added_count = 1
    FakeWeread.fail_shelf = False

    first = asyncio.run(subscribe_mp_by_article(
        request=_request(),
        payload=ArticleSubscriptionRequest(
            url="https://mp.weixin.qq.com/s/example"
        ),
        current_user={"username": "test"},
    ))
    FakeWeread.added_count = 0
    second = asyncio.run(subscribe_mp_by_article(
        request=_request(),
        payload=ArticleSubscriptionRequest(
            url="https://mp.weixin.qq.com/s/example2"
        ),
        current_user={"username": "test"},
    ))

    assert first["message"] == "订阅添加成功"
    assert first["data"]["rss_url"] == (
        "https://rss.example.com/rss/MP_WXS_3581557283"
    )
    assert first["data"]["created"] is True
    assert first["data"]["shelf_added"] is True
    assert first["data"]["initial_fetch_queued"] is True
    assert second["message"] == "订阅已存在"
    assert second["data"]["created"] is False
    assert second["data"]["shelf_added"] is False
    assert second["data"]["initial_fetch_queued"] is False
    assert len(queued) == 1
    with sessions() as session:
        assert session.query(Feed).count() == 1


def test_subscribe_by_article_rolls_back_when_shelf_add_fails(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr("apis.mps.DB.get_session", sessions)
    monkeypatch.setattr("apis.mps.WXArticleFetcher", FakeIdentityFetcher)
    monkeypatch.setattr("apis.mps.MpsWeread", FakeWeread)
    monkeypatch.setattr("apis.mps.save_avatar_locally", lambda value: value)
    FakeWeread.fail_shelf = True
    try:
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(subscribe_mp_by_article(
                request=_request(),
                payload=ArticleSubscriptionRequest(
                    url="https://mp.weixin.qq.com/s/example"
                ),
                current_user={"username": "test"},
            ))
        assert exc_info.value.status_code == 400
        with sessions() as session:
            assert session.query(Feed).count() == 0
    finally:
        FakeWeread.fail_shelf = False


def test_legacy_search_only_queries_local_subscriptions(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        session.add(Feed(
            id="MP_WXS_3581557283",
            faker_id=_encoded_numeric_id(),
            mp_name="测试公众号",
            mp_cover="cover.jpg",
            mp_intro="测试简介",
            status=1,
        ))
        session.commit()
    monkeypatch.setattr("apis.mps.DB.get_session", sessions)

    result = asyncio.run(search_mp(
        kw="测试",
        limit=10,
        offset=0,
        current_user={"username": "test"},
    ))

    assert result["data"]["total"] == 1
    assert result["data"]["list"][0]["nickname"] == "测试公众号"
    assert result["data"]["list"][0]["subscribe_payload"]["mp_id"] == (
        _encoded_numeric_id()
    )
