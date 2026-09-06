from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Db, article_id_candidates
from core.models.article import Article
from core.models.base import Base


def _database():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    database = Db.__new__(Db)
    database.get_session = session_factory
    return database, session_factory


def _article(article_id, title, url, publish_time):
    return {
        "id": article_id,
        "mp_id": "MP_WXS_3073282833",
        "title": title,
        "url": url,
        "description": title,
        "content": "",
        "status": 1,
        "publish_time": publish_time,
        "item_show_type": 0,
    }


def test_article_id_prefers_mid_and_idx_from_full_wechat_url():
    preferred, aliases = article_id_candidates(
        "MP_WXS_3073282833",
        "3073282833_short-token",
        "https://mp.weixin.qq.com/s?__biz=x&mid=2651047068&idx=2&sn=y",
    )
    assert preferred == "2651047068_2"
    assert "3073282833-3073282833_short-token" in aliases


def test_short_link_token_matches_legacy_article_without_inserting_duplicate():
    database, session_factory = _database()
    session = session_factory()
    created_at = datetime(2026, 7, 28, 23, 2, 50)
    session.add(Article(
        id="2651047068_1",
        mp_id="MP_WXS_3073282833",
        title="同一篇文章",
        url="https://mp.weixin.qq.com/s/S0PELsdyjoOzo1Ri00ziAQ",
        content="已有正文",
        status=1,
        publish_time=1_785_252_259,
        item_show_type=0,
        created_at=created_at,
    ))
    session.commit()
    session.close()

    created = database.add_article(_article(
        "3073282833_S0PELsdyjoOzo1Ri00ziAQ",
        "同一篇文章",
        "",
        1_785_252_329,
    ))

    session = session_factory()
    rows = session.query(Article).all()
    assert created is False
    assert len(rows) == 1
    assert rows[0].id == "2651047068_1"
    assert rows[0].url == "https://mp.weixin.qq.com/s/S0PELsdyjoOzo1Ri00ziAQ"
    assert rows[0].content == "已有正文"
    assert rows[0].created_at == created_at


def test_weread_publish_time_drift_matches_legacy_article():
    database, session_factory = _database()
    session = session_factory()
    session.add(Article(
        id="2651051744_1",
        mp_id="MP_WXS_3073282833",
        title="同一篇历史文章",
        url=(
            "https://mp.weixin.qq.com/s?__biz=x&mid=2651051744"
            "&idx=1&sn=legacy"
        ),
        status=1,
        publish_time=1_787_363_688,
        item_show_type=0,
    ))
    session.commit()
    session.close()

    created = database.add_article(_article(
        "3073282833_xloHYEb6GThsgMbzcQL0SQ",
        "同一篇历史文章",
        "https://mp.weixin.qq.com/s/xloHYEb6GThsgMbzcQL0SQ",
        1_787_364_072,
    ))

    session = session_factory()
    rows = session.query(Article).all()
    assert created is False
    assert len(rows) == 1
    assert rows[0].id == "2651051744_1"
    assert "mid=2651051744" in rows[0].url


def test_empty_urls_do_not_make_different_articles_match():
    database, session_factory = _database()
    assert database.add_article(_article("first-token", "第一篇", "", 1000)) is True
    assert database.add_article(_article("second-token", "第二篇", "", 2000)) is True
    session = session_factory()
    assert session.query(Article).count() == 2


def test_repeated_title_outside_time_tolerance_is_not_deduplicated():
    database, session_factory = _database()
    assert database.add_article(_article("first-token", "每周回顾", "", 1000)) is True
    assert database.add_article(_article("second-token", "每周回顾", "", 100_000)) is True
    session = session_factory()
    assert session.query(Article).count() == 2
