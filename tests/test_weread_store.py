import time

from driver.weread_store import WereadSessionStore


def test_weread_store_detects_expiring_session_and_merges_refresh(tmp_path):
    store = WereadSessionStore(str(tmp_path / "cookies.lic"), use_redis=False)
    store.save([
        {
            "name": "wr_rt",
            "value": "refresh",
            "domain": ".weread.qq.com",
            "path": "/",
            "expires": time.time() + 86400,
        },
        {
            "name": "wr_skey",
            "value": "old",
            "domain": ".weread.qq.com",
            "path": "/",
            "expires": time.time() + 60,
        },
    ])

    assert store.needs_renewal(within_seconds=300) is True

    store.merge([{
        "name": "wr_skey",
        "value": "new",
        "domain": ".weread.qq.com",
        "path": "/",
        "expires": time.time() + 900,
    }])

    assert store.needs_renewal(within_seconds=300) is False
    assert "wr_skey=new" in store.cookie_header()


def test_weread_store_does_not_renew_without_active_refresh_cookie(tmp_path):
    store = WereadSessionStore(str(tmp_path / "cookies.lic"), use_redis=False)
    store.save([{
        "name": "wr_skey",
        "value": "old",
        "domain": ".weread.qq.com",
        "path": "/",
        "expires": time.time() - 1,
    }])

    assert store.needs_renewal() is False
