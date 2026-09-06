import json

import pytest

from core.wx.model.weread import (
    MpsWeread,
    WereadAuthenticationError,
)


class FakeResponse:
    def __init__(self, payload=None, text="", status_code=200, cookies=None):
        self.payload = payload
        self.text = text
        self.status_code = status_code
        self.cookies = list(cookies or [])

    def json(self):
        if self.payload is None:
            raise json.JSONDecodeError("not json", self.text, 0)
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _model(responses, cookie="test-cookie"):
    model = MpsWeread(session=FakeSession(responses), cookie=cookie)
    model.Gather_Content = False
    model.update_mps = lambda *args, **kwargs: None
    model.Over = lambda CallBack=None: CallBack(model.articles) if CallBack else None
    return model


class FakeStore:
    def __init__(self, cookies, needs_renewal=False):
        self.cookies = list(cookies)
        self.renewal_required = needs_renewal
        self.merge_count = 0

    def cookie_header(self):
        return "; ".join(
            f"{item['name']}={item['value']}" for item in self.cookies
        )

    def needs_renewal(self, within_seconds=300):
        return self.renewal_required

    def merge(self, cookies):
        updates = {item["name"]: dict(item) for item in cookies}
        self.cookies = [
            updates.pop(item["name"], item) for item in self.cookies
        ] + list(updates.values())
        self.renewal_required = False
        self.merge_count += 1

    @staticmethod
    def _cookie_items(cookies):
        return iter(cookies or [])


def _review(review_id="review-1"):
    return {
        "reviewId": review_id,
        "type": 16,
        "createTime": 1710000000,
        "mpInfo": {
            "originalId": "original-1",
            "doc_url": "https://mp.weixin.qq.com/s/example",
            "pic_url": "https://example.com/cover.jpg",
            "title": "测试文章",
            "content": "测试摘要",
            "mp_name": "测试公众号",
            "time": 1710000000,
            "readNum": 12,
            "likeNum": 3,
            "payType": 0,
        },
    }


def test_weread_articles_map_to_existing_callback_contract():
    session_response = {
        "reviews": [{
            "createTime": 1710000000,
            "subCount": 1,
            "subReviews": [{"reviewId": "review-1", "review": _review()}],
        }],
        "clearAll": 1,
        "synckey": 1710000000,
    }
    model = _model([FakeResponse(payload=session_response)])
    received = []

    result = model.get_Articles(
        Mps_id="MP_WXS_123",
        Mps_title="测试公众号",
        CallBack=lambda article: received.append(article) or True,
        MaxPage=1,
        interval=0,
    )

    assert result == received
    assert received[0]["id"] == "review-1"
    assert received[0]["mp_id"] == "MP_WXS_123"
    assert received[0]["title"] == "测试文章"
    assert received[0]["url"] == "https://mp.weixin.qq.com/s/example"
    assert received[0]["description"] == "测试摘要"
    assert received[0]["publish_time"] == 1710000000
    assert json.loads(received[0]["publish_info"])["source"] == "weread"
    assert model.session.calls[0][1]["params"] == {
        "bookId": "MP_WXS_123",
        "offset": 0,
    }


def test_weread_skips_reviews_from_another_mp():
    foreign_review = _review("MP_WXS_999_foreign-token")
    foreign_review["mpInfo"]["mp_name"] = "其他公众号"
    session_response = {
        "reviews": [{
            "createTime": 1710000000,
            "subCount": 1,
            "subReviews": [{
                "reviewId": foreign_review["reviewId"],
                "review": foreign_review,
            }],
        }],
        "clearAll": 1,
        "synckey": 1710000000,
    }
    model = _model([FakeResponse(payload=session_response)])
    received = []

    result = model.get_Articles(
        Mps_id="MP_WXS_123",
        Mps_title="测试公众号",
        CallBack=lambda article: received.append(article) or True,
        MaxPage=1,
        interval=0,
    )

    assert result == []
    assert received == []


def test_weread_fetches_single_review_fallback_and_content(monkeypatch):
    list_response = {
        "reviews": [{
            "createTime": 1710000000,
            "subReviews": [{"reviewId": "review-1"}],
        }]
    }
    model = _model([
        FakeResponse(payload=list_response),
        FakeResponse(payload={"review": _review()}),
        FakeResponse(text="<!DOCTYPE html><html><div id='js_content'><p>正文</p></div></html>"),
    ])
    model.Gather_Content = True
    received = []

    model.get_Articles(
        Mps_id="MP_WXS_123",
        CallBack=lambda article: received.append(article) or True,
        MaxPage=1,
        interval=0,
    )

    assert "正文" in received[0]["content"]
    assert [call[0].removeprefix(model.BASE_URL) for call in model.session.calls] == [
        "/web/mp/articles",
        "/web/mp/review/single",
        "/web/mp/content",
    ]


def test_weread_always_fetches_latest_page_only():
    model = _model([FakeResponse(payload={"reviews": []})])

    model.get_Articles(
        Mps_id="MP_WXS_123",
        start_page=3,
        MaxPage=5,
        interval=0,
    )

    assert len(model.session.calls) == 1
    assert model.session.calls[0][1]["params"] == {
        "bookId": "MP_WXS_123",
        "offset": 0,
    }


def test_weread_keeps_review_id_as_stable_article_id():
    review = _review()
    model = _model([])

    article = model._article_data(
        review,
        "review-1",
        "MP_WXS_123",
        False,
    )

    assert article["id"] == "review-1"


def test_weread_normalizes_tracking_parameters_without_losing_identity():
    assert MpsWeread._normalize_doc_url(
        "https://mp.weixin.qq.com/s/short-id?scene=21&poc_token=secret#rd"
    ) == "https://mp.weixin.qq.com/s/short-id"
    assert MpsWeread._normalize_doc_url(
        "https://mp.weixin.qq.com/s?__biz=test==&mid=1&idx=2&sn=abc"
        "&chksm=def&scene=21&poc_token=secret#rd"
    ) == (
        "https://mp.weixin.qq.com/s?__biz=test==&mid=1&idx=2&sn=abc&chksm=def"
    )


def test_weread_builds_short_link_when_sub_article_has_no_doc_url():
    review = _review("3073282833_EymZna02dueCmmVtraoxTA")
    review["mpInfo"].pop("doc_url")
    review["mpInfo"]["originalId"] = "EymZna02dueCmmVtraoxTA"

    article = _model([])._article_data(
        review,
        review["reviewId"],
        "MP_WXS_3073282833",
        False,
    )

    assert article["link"] == (
        "https://mp.weixin.qq.com/s/EymZna02dueCmmVtraoxTA"
    )


def test_weread_prefers_real_publish_time_and_normalizes_milliseconds():
    review = _review()
    review["mpInfo"]["time"] = "1710000000123"
    review["createTime"] = 1800000000
    model = _model([])

    article = model._article_data(
        review,
        "review-1",
        "MP_WXS_123",
        False,
        fallback_timestamp=1900000000,
    )

    assert article["update_time"] == 1710000000


def test_weread_falls_back_to_group_time_when_article_time_is_missing():
    review = _review()
    review["mpInfo"].pop("time")
    review.pop("createTime")
    model = _model([])

    article = model._article_data(
        review,
        "review-1",
        "MP_WXS_123",
        False,
        fallback_timestamp=1710000123,
    )

    assert article["update_time"] == 1710000123


def test_weread_falls_back_to_latest_cover_when_articles_returns_2041():
    model = _model([
        FakeResponse(payload={"errCode": -2041, "errMsg": "-2041"}),
        FakeResponse(payload={
            "name": "测试公众号",
            "reviewId": "review-from-cover",
        }),
        FakeResponse(payload={"review": _review("review-from-cover")}),
    ])
    received = []

    model.get_Articles(
        Mps_id="MP_WXS_123",
        Mps_title="测试公众号",
        CallBack=lambda article: received.append(article) or True,
        MaxPage=1,
        interval=0,
    )

    assert [item["id"] for item in received] == ["review-from-cover"]
    assert [call[0].removeprefix(model.BASE_URL) for call in model.session.calls] == [
        "/web/mp/articles",
        "/web/mp/cover",
        "/web/mp/review/single",
    ]


def test_weread_cover_fallback_only_returns_first_page():
    model = _model([])

    assert model._get_cover_article_groups("MP_WXS_123", 20) == []


def test_weread_requires_cookie_before_request():
    model = _model([], cookie="")

    with pytest.raises(WereadAuthenticationError, match="扫码授权"):
        model._get_article_groups("MP_WXS_123", 0)


def test_weread_renews_expiring_session_before_request():
    store = FakeStore([
        {"name": "wr_ql", "value": "1"},
        {"name": "wr_rt", "value": "refresh"},
        {"name": "wr_skey", "value": "old"},
    ], needs_renewal=True)
    session = FakeSession([
        FakeResponse(
            payload={"succ": 1},
            cookies=[{"name": "wr_skey", "value": "new"}],
        ),
        FakeResponse(payload={"books": []}),
    ])
    model = MpsWeread(session=session, store=store)

    assert model.get_shelf_book_ids() == set()
    assert session.calls[0][0].endswith("/web/login/renewal")
    assert session.calls[0][1]["json"] == {
        "rq": "%2Fweb%2Fshelf%2Fsync",
        "ql": True,
    }
    assert "wr_skey=new" in session.calls[1][1]["headers"]["Cookie"]
    assert store.merge_count == 1


def test_weread_renews_and_retries_once_after_session_error():
    store = FakeStore([
        {"name": "wr_ql", "value": "1"},
        {"name": "wr_rt", "value": "refresh"},
        {"name": "wr_skey", "value": "old"},
    ])
    session = FakeSession([
        FakeResponse(payload={"errCode": -2010}),
        FakeResponse(
            payload={"succ": 1},
            cookies=[{"name": "wr_skey", "value": "new"}],
        ),
        FakeResponse(payload={"books": [{"bookId": "MP_WXS_1"}]}),
    ])
    model = MpsWeread(session=session, store=store)

    assert model.get_shelf_book_ids() == {"MP_WXS_1"}
    assert [call[0].removeprefix(model.BASE_URL) for call in session.calls] == [
        "/web/shelf/sync",
        "/web/login/renewal",
        "/web/shelf/sync",
    ]


def test_weread_renewal_failure_requires_new_qr_login():
    store = FakeStore([
        {"name": "wr_ql", "value": "1"},
        {"name": "wr_rt", "value": "refresh"},
        {"name": "wr_skey", "value": "old"},
    ], needs_renewal=True)
    model = MpsWeread(
        session=FakeSession([FakeResponse(payload={"errCode": -2010})]),
        store=store,
    )

    with pytest.raises(WereadAuthenticationError, match="重新扫码授权"):
        model.get_shelf_book_ids()


def test_weread_stops_after_one_unsuccessful_retry():
    store = FakeStore([
        {"name": "wr_ql", "value": "1"},
        {"name": "wr_rt", "value": "refresh"},
        {"name": "wr_skey", "value": "old"},
    ])
    model = MpsWeread(
        session=FakeSession([
            FakeResponse(payload={"errCode": -2010}),
            FakeResponse(
                payload={"succ": 1},
                cookies=[{"name": "wr_skey", "value": "new"}],
            ),
            FakeResponse(payload={"errCode": -2010}),
        ]),
        store=store,
    )

    with pytest.raises(WereadAuthenticationError, match="仍未登录"):
        model.get_shelf_book_ids()
    assert len(model.session.calls) == 3


def test_weread_add_to_shelf_uses_verified_endpoint():
    model = _model([FakeResponse(payload={"succ": 1})])

    result = model.add_to_shelf(["MP_WXS_123", "MP_WXS_123"])

    assert result == {"succ": 1, "book_ids": ["MP_WXS_123"]}
    assert model.session.calls == [(
        "https://weread.qq.com/web/shelf/add",
        {
            "json": {"bookIds": ["MP_WXS_123"]},
            "headers": model.headers,
            "proxies": None,
            "timeout": (10, 30),
        },
    )]


def test_weread_shelf_ids_include_books_mp_and_archives():
    model = _model([FakeResponse(payload={
        "books": [
            {"bookId": "MP_WXS_1"},
            {"bookId": "normal-book"},
        ],
        "mp": {"book": {"bookId": "MP_WXS_2"}},
        "archive": [{"bookIds": ["MP_WXS_3", "MP_WXS_1"]}],
    })])

    result = model.get_shelf_book_ids()

    assert result == {"MP_WXS_1", "MP_WXS_2", "MP_WXS_3", "normal-book"}
    assert model.session.calls[0][0] == "https://weread.qq.com/web/shelf/sync"
    assert model.session.calls[0][1]["params"] == {
        "synckey": 0,
        "lectureSynckey": 0,
    }


def test_weread_add_missing_to_shelf_skips_existing_and_deduplicates():
    model = _model([
        FakeResponse(payload={"books": [{"bookId": "MP_WXS_1"}]}),
        FakeResponse(payload={"succ": 1}),
    ])

    result = model.add_missing_to_shelf([
        "MP_WXS_1",
        "MP_WXS_2",
        "MP_WXS_2",
    ])

    assert result == {
        "succ": 1,
        "book_ids": ["MP_WXS_1", "MP_WXS_2"],
        "added_book_ids": ["MP_WXS_2"],
        "existing_book_ids": ["MP_WXS_1"],
        "added_count": 1,
        "existing_count": 1,
        "total": 2,
    }
    assert len(model.session.calls) == 2
    assert model.session.calls[1][1]["json"] == {"bookIds": ["MP_WXS_2"]}


def test_weread_add_missing_to_shelf_does_not_post_when_all_exist():
    model = _model([FakeResponse(payload={
        "books": [{"bookId": "MP_WXS_1"}],
    })])

    result = model.add_missing_to_shelf(["MP_WXS_1", "MP_WXS_1"])

    assert result["added_count"] == 0
    assert result["existing_count"] == 1
    assert len(model.session.calls) == 1


def test_weread_model_selection(monkeypatch):
    original_get = __import__("core.wx.base", fromlist=["cfg"]).cfg.get
    monkeypatch.setattr(
        "core.wx.base.cfg.get",
        lambda key, default=None: "weread" if key == "gather.model" else original_get(key, default),
    )

    assert isinstance(MpsWeread().Model(), MpsWeread)
