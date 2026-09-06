from datetime import date, datetime

from core.db import DB
from core.log import logger
from core.models.article import Article
from core.models.feed import FEATURED_MP_ID, Feed

FREQUENCY_TWICE_DAILY = "twice_daily"
FREQUENCY_DAILY = "daily"
FREQUENCY_THREE_DAY = "three_day"
VALID_FREQUENCIES = {
    FREQUENCY_TWICE_DAILY,
    FREQUENCY_DAILY,
    FREQUENCY_THREE_DAY,
}
STATS_WINDOW_DAYS = 28
MIN_STATS_DAYS = 14
MIN_STATS_ARTICLES = 3


def normalize_frequency(value):
    value = str(value or "").strip().lower()
    return value if value in VALID_FREQUENCIES else FREQUENCY_DAILY


def mark_feed_result(mp_id: str, success: bool, now_ts: int | None = None):
    session = DB.get_session()
    try:
        feed = session.query(Feed).filter(Feed.id == mp_id).first()
        if not feed:
            return
        if success:
            feed.consecutive_failures = 0
            feed.last_success_sync_time = now_ts or int(datetime.now().timestamp())
        else:
            feed.consecutive_failures = (feed.consecutive_failures or 0) + 1
        feed.updated_at = datetime.now()
        session.commit()
    finally:
        session.close()


def get_feeds_by_frequency(frequency: str):
    session = DB.get_session()
    try:
        frequency = normalize_frequency(frequency)
        feeds = session.query(Feed).filter(
            Feed.status == 1,
            Feed.id != FEATURED_MP_ID,
        ).all()
        return [
            feed for feed in feeds
            if normalize_frequency(feed.frequency) == frequency
        ]
    finally:
        session.close()


def analyze_feed_frequency(feed: Feed, now_ts: int | None = None, session=None):
    now_ts = now_ts or int(datetime.now().timestamp())
    if (feed.consecutive_failures or 0) > 0:
        return None, "consecutive_failures"

    last_success = feed.last_success_sync_time or feed.sync_time or feed.update_time
    if last_success and int(last_success) < now_ts - MIN_STATS_DAYS * 86400:
        return None, "no_recent_success"

    owns_session = session is None
    session = session or DB.get_session()
    try:
        window_start = now_ts - STATS_WINDOW_DAYS * 86400
        rows = session.query(Article.publish_time).filter(
            Article.mp_id == feed.id,
            Article.publish_time >= window_start,
            Article.publish_time <= now_ts,
        ).all()
        publish_times = [int(row[0]) for row in rows if row[0]]
    finally:
        if owns_session:
            session.close()

    if len(publish_times) < MIN_STATS_ARTICLES:
        return None, "insufficient_articles"
    observed_days = min(
        STATS_WINDOW_DAYS,
        max(1, (now_ts - min(publish_times)) // 86400 + 1),
    )
    if observed_days < MIN_STATS_DAYS:
        return None, "insufficient_days"

    active_days = {date.fromtimestamp(value).isoformat() for value in publish_times}
    average_per_day = len(publish_times) / observed_days
    active_day_ratio = len(active_days) / observed_days
    if average_per_day >= 1.5:
        return FREQUENCY_TWICE_DAILY, "multiple_updates_daily"
    if active_day_ratio >= 0.65 or average_per_day >= 0.8:
        return FREQUENCY_DAILY, "updates_daily"
    return FREQUENCY_THREE_DAY, "not_daily"


def update_frequency_statistics(dry_run: bool = False):
    session = DB.get_session()
    try:
        feeds = session.query(Feed).filter(
            Feed.status == 1,
            Feed.id != FEATURED_MP_ID,
        ).all()
        result = {
            "checked": len(feeds),
            "updated": 0,
            "skipped": {},
            "tiers": {value: 0 for value in VALID_FREQUENCIES},
        }
        for feed in feeds:
            frequency, reason = analyze_feed_frequency(feed, session=session)
            if not frequency:
                result["skipped"][reason] = result["skipped"].get(reason, 0) + 1
                continue
            result["tiers"][frequency] += 1
            if normalize_frequency(feed.frequency) == frequency:
                continue
            result["updated"] += 1
            if not dry_run:
                feed.frequency = frequency
                feed.updated_at = datetime.now()
        if not dry_run:
            session.commit()
        logger.info(f"公众号抓取频率统计完成: {result}")
        return result
    finally:
        session.close()
