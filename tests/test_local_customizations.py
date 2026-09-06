import base64
import xml.etree.ElementTree as ET
from datetime import datetime
from types import SimpleNamespace

from bs4 import BeautifulSoup
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from apis.mps import (
    MpConfirmRequest,
    _confirm_mp_subscription,
    _decode_mp_id,
    _search_item_payload,
    _serialize_mp,
)
from core.config import cfg
from core.db import Db
from core.models.article import Article
from core.models.base import Base
from core.models.feed import Feed
from core.rss import CONTENT_NAMESPACE, RSS
from core.wx.content import parse_article_content
from jobs.feed_frequency import (
    FREQUENCY_DAILY,
    FREQUENCY_THREE_DAY,
    FREQUENCY_TWICE_DAILY,
    analyze_feed_frequency,
    normalize_frequency,
)
from tools.migrate_legacy_config import migrate_config, migrate_license


def test_picture_article_static_parser_keeps_main_images_only():
    content = parse_article_content(r"""
        <script>
        window.picture_page_info_list = [{
          width: '1320' * 1,
          height: '2868' * 1,
          cdn_url: 'https://mmbiz.qpic.cn/main-1/0?wx_fmt=jpeg\x26amp;tp=webp',
          watermark_info: {cdn_url: 'https://mmbiz.qpic.cn/watermark/0'}
        }, {
          cdn_url: '//mmbiz.qpic.cn/main-2/0?wx_fmt=png',
          width: '800' * 1,
          height: '600' * 1
        }];
        window.desc = "第一段\x0a第二段 &amp; 更多";
        </script>
    """)
    soup = BeautifulSoup(content, "html.parser")
    images = soup.find(id="js_content").find_all("img")
    assert [image["src"] for image in images] == [
        "https://mmbiz.qpic.cn/main-1/0?wx_fmt=jpeg&tp=webp",
        "https://mmbiz.qpic.cn/main-2/0?wx_fmt=png",
    ]
    assert "watermark" not in content
    assert "第二段 & 更多" in soup.get_text()


def test_standard_article_static_parser_rewrites_lazy_image():
    content = parse_article_content(
        '<div id="js_content" style="visibility:hidden">'
        '<img data-src="https://example.com/a.jpg" style="width:640px"></div>'
    )
    image = BeautifulSoup(content, "html.parser").find("img")
    assert image["src"] == "https://example.com/a.jpg"
    assert "data-src" not in image.attrs
    assert "width: 1080px" in image["style"]


def test_rss_full_content_uses_valid_namespace(tmp_path):
    original = cfg.config
    cfg.config = {"rss": {"full_context": True, "cdata": False}}
    try:
        rss = RSS("test", cache_dir=str(tmp_path))
        output = rss.generate_rss([{
            "id": "1",
            "title": "标题",
            "description": "摘要",
            "content": "<p>正文</p>",
            "link": "https://example.com/1",
            "updated": datetime.now(),
        }])
    finally:
        cfg.config = original
    root = ET.fromstring(output)
    encoded = root.find(f".//{{{CONTENT_NAMESPACE}}}encoded")
    assert encoded is not None
    assert encoded.text == "<p>正文</p>"
    assert output.count("xmlns:content=") == 1


def test_rss_uses_article_id_when_link_is_empty(tmp_path):
    original = cfg.config
    cfg.config = {"rss": {"full_context": False}}
    try:
        rss = RSS("test-empty-link", cache_dir=str(tmp_path))
        output = rss.generate_rss([{
            "id": "stable-article-id",
            "title": "标题",
            "description": "摘要",
            "content": "",
            "link": "",
            "updated": datetime.now(),
        }])
    finally:
        cfg.config = original
    guid = ET.fromstring(output).find(".//guid")
    assert guid is not None
    assert guid.text == "stable-article-id"
    assert guid.attrib == {"isPermaLink": "false"}


def test_rss_keeps_existing_link_guid(tmp_path):
    original = cfg.config
    cfg.config = {"rss": {"full_context": False}}
    try:
        rss = RSS("test-link-guid", cache_dir=str(tmp_path))
        output = rss.generate_rss([{
            "id": "stable-article-id",
            "title": "标题",
            "description": "摘要",
            "content": "",
            "link": "https://example.com/article",
            "updated": datetime.now(),
        }])
    finally:
        cfg.config = original
    guid = ET.fromstring(output).find(".//guid")
    assert guid is not None
    assert guid.text == "https://example.com/article"
    assert guid.attrib == {}


def test_external_api_compatibility_helpers(monkeypatch):
    encoded = base64.b64encode(b"fake-id").decode()
    assert _decode_mp_id(encoded) == "fake-id"
    assert _decode_mp_id("not-base64") is None
    assert _search_item_payload({
        "nickname": "名称",
        "fakeid": encoded,
        "round_head_img": "https://example.com/avatar.jpg",
        "signature": "简介",
    }) == {
        "mp_name": "名称",
        "mp_id": encoded,
        "avatar": "https://example.com/avatar.jpg",
        "mp_cover": "https://example.com/avatar.jpg",
        "mp_intro": "简介",
    }
    monkeypatch.setattr(cfg, "config", {"rss": {"base_url": "https://rss.example"}})
    item = _serialize_mp(SimpleNamespace(
        id="MP_WXS_fake-id",
        mp_name="名称",
        mp_cover="cover",
        mp_intro="简介",
        status=1,
        frequency="high",
        update_time=123,
        sync_time=0,
        created_at=None,
        updated_at=None,
    ))
    assert item["frequency"] == FREQUENCY_DAILY
    assert item["rss_url"] == "https://rss.example/rss/MP_WXS_fake-id"


def test_confirm_subscription_is_idempotent(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    monkeypatch.setattr("apis.mps.save_avatar_locally", lambda value: value)
    encoded = base64.b64encode(b"fake-id").decode()
    payload = MpConfirmRequest(
        mp_name="首次名称",
        mp_id=encoded,
        avatar="avatar",
        mp_intro="简介",
    )
    feed, created = _confirm_mp_subscription(session, payload)
    session.commit()
    assert created is True
    payload.mp_name = "更新名称"
    feed, created = _confirm_mp_subscription(session, payload)
    assert created is False
    assert feed.mp_name == "更新名称"


def _frequency_session(article_days):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now_ts = 2_000_000_000
    feed = Feed(
        id="feed",
        mp_name="测试",
        status=1,
        frequency=FREQUENCY_DAILY,
        last_success_sync_time=now_ts,
    )
    session.add(feed)
    for index, day in enumerate(article_days):
        session.add(Article(
            id=f"a-{index}",
            mp_id=feed.id,
            title=f"文章{index}",
            url=f"https://example.com/{index}",
            publish_time=now_ts - day * 86400,
        ))
    session.commit()
    return session, feed, now_ts


def test_adaptive_frequency_boundaries():
    assert normalize_frequency("high") == FREQUENCY_DAILY
    session, feed, now_ts = _frequency_session(
        [day for day in range(28) for _ in range(2)]
    )
    assert analyze_feed_frequency(feed, now_ts, session)[0] == FREQUENCY_TWICE_DAILY
    session.close()

    session, feed, now_ts = _frequency_session(range(0, 28, 4))
    assert analyze_feed_frequency(feed, now_ts, session)[0] == FREQUENCY_THREE_DAY
    feed.consecutive_failures = 1
    assert analyze_feed_frequency(feed, now_ts, session) == (None, "consecutive_failures")
    session.close()


def test_legacy_config_migration_enables_full_content():
    migrated = migrate_config(
        {
            "db": "sqlite:///db.db",
            "gather_content": False,
            "model": "api",
            "token": "secret-token",
            "cookie": "secret-cookie",
        },
        {
            "db": "sqlite:///data/db.db",
            "gather": {"content": False, "model": "web", "content_fetch_timeout": 60},
            "rss": {"full_context": False},
            "server": {"enable_job": True},
        },
    )
    assert migrated["db"] == "sqlite:///db.db"
    assert migrated["gather"]["content"] is True
    assert migrated["gather"]["content_auto_check"] is False
    assert migrated["gather"]["model"] == "api"
    assert migrated["gather"]["content_fetch_timeout"] == 60
    assert migrated["rss"]["full_context"] is True
    assert migrated["server"]["enable_job"] is True
    assert migrated["redis"]["url"] == ""
    assert migrated["redis"]["server"]["enabled"] is False
    assert migrate_license(
        {"token": "secret-token", "cookie": "secret-cookie"}
    )["token_data"]["cookie"] == "secret-cookie"


def test_legacy_feed_schema_migration_is_idempotent():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE feeds ("
            "id VARCHAR(255) PRIMARY KEY, frequency VARCHAR(20), "
            "consecutive_failures INTEGER)"
        ))
        connection.execute(text(
            "INSERT INTO feeds (id, frequency, consecutive_failures) "
            "VALUES ('legacy', 'high', NULL)"
        ))
    database = Db.__new__(Db)
    database.engine = engine
    database.tag = "测试"
    database.ensure_feed_columns()
    database.ensure_feed_columns()
    columns = {column["name"] for column in inspect(engine).get_columns("feeds")}
    assert {"frequency", "consecutive_failures", "last_success_sync_time"} <= columns
    with engine.connect() as connection:
        row = connection.execute(text(
            "SELECT frequency, consecutive_failures FROM feeds WHERE id = 'legacy'"
        )).one()
    assert row == ("daily", 0)


def test_legacy_message_task_schema_migration_is_idempotent():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE message_tasks ("
            "id VARCHAR(255) PRIMARY KEY, name VARCHAR(100))"
        ))
        connection.execute(text(
            "INSERT INTO message_tasks (id, name) VALUES ('legacy', '旧任务')"
        ))
    database = Db.__new__(Db)
    database.engine = engine
    database.tag = "测试"
    database.ensure_message_task_columns()
    database.ensure_message_task_columns()
    columns = {
        column["name"]
        for column in inspect(engine).get_columns("message_tasks")
    }
    assert {"headers", "cookies"} <= columns
    with engine.connect() as connection:
        row = connection.execute(text(
            "SELECT id, name, headers, cookies FROM message_tasks"
        )).one()
    assert row == ("legacy", "旧任务", None, None)


def test_adaptive_job_does_not_require_message_task(monkeypatch):
    from jobs import mps
    results = []

    class FakeGather:
        articles = []

        def Model(self):
            return self

        def get_Articles(self, *args, **kwargs):
            return None

        def all_count(self):
            return 0

    monkeypatch.setattr(mps, "WxGather", FakeGather)
    monkeypatch.setattr(
        "jobs.feed_frequency.mark_feed_result",
        lambda mp_id, success: results.append((mp_id, success)),
    )
    feed = SimpleNamespace(id="feed", faker_id="encoded", mp_name="测试")
    mps.do_job(feed, task=None)
    assert results == [("feed", True)]
