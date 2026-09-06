import json
import xml.etree.ElementTree as ET
from datetime import datetime
from types import SimpleNamespace

from core.config import cfg
from core.rss import RSS, article_rss_link


def test_empty_source_url_falls_back_to_local_article_page():
    article = SimpleNamespace(id="stable-article-id", url="", publish_info="")

    assert article_rss_link(article, "https://rss.example/") == (
        "https://rss.example/views/article/stable-article-id"
    )


def test_legacy_weread_article_recovers_short_link_from_publish_info():
    article = SimpleNamespace(
        id="3073282833_EymZna02dueCmmVtraoxTA",
        url="",
        publish_info=json.dumps({
            "source": "weread",
            "original_id": "EymZna02dueCmmVtraoxTA",
        }),
    )

    assert article_rss_link(article, "https://rss.example/") == (
        "https://mp.weixin.qq.com/s/EymZna02dueCmmVtraoxTA"
    )


def test_source_url_is_kept_for_external_rss_items():
    article = SimpleNamespace(
        id="stable-article-id",
        url="https://mp.weixin.qq.com/s/article-token",
        publish_info="",
    )

    assert article_rss_link(article, "https://rss.example/") == article.url


def test_recovered_link_keeps_legacy_article_id_as_guid(tmp_path):
    original = cfg.config
    cfg.config = {"rss": {"full_context": False}}
    try:
        output = RSS("legacy-guid", cache_dir=str(tmp_path)).generate_rss([{
            "id": "3073282833_EymZna02dueCmmVtraoxTA",
            "title": "历史子文章",
            "description": "摘要",
            "content": "",
            "link": "https://mp.weixin.qq.com/s/EymZna02dueCmmVtraoxTA",
            "guid": "3073282833_EymZna02dueCmmVtraoxTA",
            "guid_is_permalink": False,
            "updated": datetime.now(),
        }])
    finally:
        cfg.config = original

    root = ET.fromstring(output)
    assert root.findtext(".//item/link") == (
        "https://mp.weixin.qq.com/s/EymZna02dueCmmVtraoxTA"
    )
    guid = root.find(".//guid")
    assert guid.text == "3073282833_EymZna02dueCmmVtraoxTA"
    assert guid.attrib == {"isPermaLink": "false"}
