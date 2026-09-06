import asyncio
from pathlib import Path

from driver.weread import WereadLoginService


class FakeStore:
    def __init__(self, cookies=None):
        self.cookies = list(cookies or [])

    def save(self, cookies):
        self.cookies = list(cookies)

    def load(self):
        return list(self.cookies)

    def cookie_header(self):
        return "; ".join(
            f"{item['name']}={item['value']}" for item in self.cookies
        )

    def has_auth_cookie(self):
        names = {
            item.get("name") for item in self.cookies if item.get("value")
        }
        return {"wr_localvid", "wr_ql"}.issubset(names)

    def clear(self):
        self.cookies = []


class FakeLocator:
    def __init__(self, page):
        self.page = page

    async def is_visible(self):
        return not self.page.authenticated

    async def wait_for(self, **kwargs):
        return None

    async def screenshot(self, path):
        Path(path).write_bytes(b"q" * 512)


class FakePage:
    def __init__(self, authenticated=False):
        self.authenticated = authenticated
        self.scanned = asyncio.Event()

    async def evaluate(self, script):
        return self.authenticated

    def locator(self, selector):
        assert selector == 'img[alt="扫码登录"]'
        return FakeLocator(self)

    async def wait_for_function(self, script, timeout):
        await self.scanned.wait()
        self.authenticated = True


class FakeController:
    init_kwargs = None
    page_authenticated = False
    latest = None

    def __init__(self, **kwargs):
        type(self).init_kwargs = kwargs
        self.page = FakePage(authenticated=type(self).page_authenticated)
        type(self).latest = self

    async def start_browser(self):
        return None

    async def add_cookies(self, cookies):
        return None

    async def open_url(self, *args, **kwargs):
        return True

    async def get_cookies(self):
        if self.page.scanned.is_set():
            self.page.authenticated = True
        cookies = [{
            "name": "wr_localvid",
            "value": "123",
            "domain": ".weread.qq.com",
            "path": "/",
        }]
        if self.page.authenticated:
            cookies.append({
                "name": "wr_ql",
                "value": "1",
                "domain": ".weread.qq.com",
                "path": "/",
            })
        return cookies

    async def close(self):
        return None


def test_weread_qr_login_exposes_status_and_persists_cookies(tmp_path):
    async def scenario():
        FakeController.page_authenticated = False
        store = FakeStore()
        service = WereadLoginService(
            controller_factory=FakeController,
            store=store,
            qr_path=str(tmp_path / "weread.png"),
        )

        status = await service.start_login()
        assert status["state"] == "waiting_scan"
        assert status["login_status"] is False
        assert status["qr_code"].startswith("/static/weread_qrcode.png")
        assert FakeController.init_kwargs["apply_anti_crawler"] is False

        FakeController.latest.page.scanned.set()
        await service.task

        assert service.status()["state"] == "confirmed"
        assert service.status()["login_status"] is True
        assert store.cookies[0]["name"] == "wr_localvid"
        assert not (tmp_path / "weread.png").exists()

        result = await service.unbind()
        assert result["state"] == "idle"
        assert result["login_status"] is False

    asyncio.run(scenario())


def test_weread_saved_session_skips_qr(tmp_path):
    async def scenario():
        FakeController.page_authenticated = True
        store = FakeStore([{
            "name": "wr_localvid",
            "value": "123",
            "domain": ".weread.qq.com",
            "path": "/",
        }, {
            "name": "wr_ql",
            "value": "1",
            "domain": ".weread.qq.com",
            "path": "/",
        }])
        service = WereadLoginService(
            controller_factory=FakeController,
            store=store,
            qr_path=str(tmp_path / "weread.png"),
        )

        status = await service.start_login()

        assert status["state"] == "confirmed"
        assert status["login_status"] is True
        assert status["qr_code"] is None

    asyncio.run(scenario())


def test_weread_status_ignores_non_auth_cookies(tmp_path):
    store = FakeStore([{
        "name": "pgv_pvid",
        "value": "123",
        "domain": ".weread.qq.com",
        "path": "/",
    }])
    service = WereadLoginService(
        controller_factory=FakeController,
        store=store,
        qr_path=str(tmp_path / "weread.png"),
    )

    assert service.status()["login_status"] is False


def test_weread_status_ignores_anonymous_localvid_cookie(tmp_path):
    store = FakeStore([{
        "name": "wr_localvid",
        "value": "123",
        "domain": ".weread.qq.com",
        "path": "/",
    }])
    service = WereadLoginService(
        controller_factory=FakeController,
        store=store,
        qr_path=str(tmp_path / "weread.png"),
    )

    assert service.status()["login_status"] is False


def test_weread_stale_qr_is_replaced(tmp_path):
    async def scenario():
        FakeController.page_authenticated = False
        service = WereadLoginService(
            controller_factory=FakeController,
            store=FakeStore(),
            qr_path=str(tmp_path / "weread.png"),
        )

        first = await service.start_login()
        first_task = service.task
        assert first["state"] == "waiting_scan"

        service.updated_at -= service.QR_MAX_AGE_SECONDS
        second = await service.start_login()

        assert second["state"] == "waiting_scan"
        assert service.task is not first_task
        await service.unbind()

    asyncio.run(scenario())
