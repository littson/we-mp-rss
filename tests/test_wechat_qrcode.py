import asyncio
from threading import Lock

import driver.wx as wx_module


class _FakePage:
    async def wait_for_load_state(self, *args, **kwargs):
        return None

    async def query_selector(self, selector):
        return _FakeQrCode()

    async def wait_for_url(self, *args, **kwargs):
        raise RuntimeError("stop after qr creation")

    def on(self, *args, **kwargs):
        return None


class _FakeQrCode:
    async def get_attribute(self, name):
        return "https://example.com/qrcode"

    async def screenshot(self, path):
        return None


class _FakeController:
    init_kwargs = None

    def __init__(self, **kwargs):
        type(self).init_kwargs = kwargs
        self.page = _FakePage()

    async def start_browser(self):
        return None

    async def open_url(self, url):
        return True

    def is_page_valid(self):
        return self.page is not None

    async def Close(self):
        return None


def _make_wx(monkeypatch):
    instance = wx_module.Wx.__new__(wx_module.Wx)
    instance._login_lock = Lock()
    instance._haslogin = False
    instance.HasCode = False
    instance.QrState = "preparing"
    instance.QrError = None
    instance.Notice = None
    instance.SESSION = None
    instance.WX_LOGIN = "https://mp.weixin.qq.com/"
    instance.WX_HOME = "https://mp.weixin.qq.com/cgi-bin/home"
    instance.wx_login_url = "unused-qrcode.png"
    monkeypatch.setattr(instance, "check_lock", lambda: False)
    monkeypatch.setattr(instance, "set_lock", lambda: None)
    monkeypatch.setattr(instance, "release_lock", lambda: None)
    monkeypatch.setattr(instance, "cleanup_resources", lambda: True)
    monkeypatch.setattr(instance, "Clean", lambda: None)
    monkeypatch.setattr(instance, "_wait_qrcode_ready", _ready)
    monkeypatch.setattr(wx_module, "PlaywrightController", _FakeController)
    monkeypatch.setattr(wx_module.os.path, "getsize", lambda path: 1024)
    return instance


async def _ready(page, selector):
    return True


def test_wechat_login_disables_anti_crawler_and_exposes_failure(monkeypatch):
    instance = _make_wx(monkeypatch)

    asyncio.run(instance.wxLogin(NeedExit=True))

    assert _FakeController.init_kwargs == {"apply_anti_crawler": False}
    assert instance.QrState == "failed"
    assert instance.QrError == "stop after qr creation"
    assert instance.QrStatus() == {
        "login_status": False,
        "qr_code": False,
        "state": "failed",
        "error": "stop after qr creation",
    }
