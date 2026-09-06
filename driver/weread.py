import asyncio
import os
import time

from core.config import cfg
from core.print import print_error, print_info, print_success
from driver.playwright_driver import PlaywrightController
from driver.weread_store import WereadStore


class WereadLoginService:
    LOGIN_URL = "https://weread.qq.com/web/shelf#login"
    QR_SELECTOR = 'img[alt="扫码登录"]'
    AUTH_COOKIE = "wr_localvid"
    QR_MAX_AGE_SECONDS = 120

    def __init__(self, controller_factory=None, store=None, qr_path=None):
        self.controller_factory = controller_factory or PlaywrightController
        self.store = store or WereadStore
        self.qr_path = qr_path or "static/weread_qrcode.png"
        self.controller = None
        self.task = None
        self.state = "idle"
        self.error = None
        self.updated_at = None
        self._lock = asyncio.Lock()
        self._ready_event = None

    async def start_login(self, force=False):
        async with self._lock:
            task_running = self.task is not None and not self.task.done()
            if task_running and (force or self._qr_is_stale()):
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass
                self.task = None
                task_running = False
            if task_running:
                ready_event = self._ready_event
            else:
                if force:
                    self.store.clear()
                self._ready_event = asyncio.Event()
                ready_event = self._ready_event
                self.state = "preparing"
                self.error = None
                self.updated_at = int(time.time())
                self.task = asyncio.create_task(self._run_login())
        try:
            await asyncio.wait_for(ready_event.wait(), timeout=30)
        except asyncio.TimeoutError:
            self.state = "failed"
            self.error = "微信读书二维码生成超时"
        return self.status()

    def _qr_is_stale(self):
        return bool(
            self.state == "waiting_scan"
            and self.updated_at
            and time.time() - self.updated_at >= self.QR_MAX_AGE_SECONDS
        )

    async def _run_login(self):
        try:
            self._remove_qr()
            self.controller = self.controller_factory(
                apply_anti_crawler=False,
                browser_type=cfg.get(
                    "weread.browser_type",
                    cfg.get("gather.browser_type", "firefox"),
                ),
            )
            await self.controller.start_browser()
            saved_cookies = self.store.load()
            if saved_cookies:
                await self.controller.add_cookies(saved_cookies)
            opened = await self.controller.open_url(
                self.LOGIN_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            if not opened or not self.controller.page:
                raise RuntimeError("微信读书登录页打开失败")
            page = self.controller.page
            if await self._page_is_authenticated(page):
                await self._complete_login()
                return

            qr = page.locator(self.QR_SELECTOR)
            await qr.wait_for(state="visible", timeout=30000)
            os.makedirs(os.path.dirname(self.qr_path) or ".", exist_ok=True)
            await qr.screenshot(path=self.qr_path)
            if not os.path.exists(self.qr_path) or os.path.getsize(self.qr_path) < 256:
                raise RuntimeError("微信读书二维码图片生成失败")
            self.state = "waiting_scan"
            self.updated_at = int(time.time())
            self._ready_event.set()
            print_info("微信读书二维码已生成，等待扫码")

            await self._wait_for_authentication(page)
            await self._complete_login()
        except asyncio.CancelledError:
            self.state = "idle"
            raise
        except Exception as exc:
            self.state = "expired" if "Timeout" in type(exc).__name__ else "failed"
            self.error = str(exc) or "微信读书登录失败"
            self.updated_at = int(time.time())
            print_error(f"微信读书登录失败: {self.error}")
        finally:
            if self._ready_event is not None:
                self._ready_event.set()
            if self.controller is not None:
                await self.controller.close()
                self.controller = None

    async def _complete_login(self):
        cookies = await self.controller.get_cookies()
        self.store.save(cookies)
        self.state = "confirmed"
        self.error = None
        self.updated_at = int(time.time())
        self._remove_qr()
        if self._ready_event is not None:
            self._ready_event.set()
        print_success("微信读书登录成功")

    async def _page_is_authenticated(self, page):
        cookies = await self.controller.get_cookies()
        if not self._has_auth_cookies(cookies):
            return False
        return not await page.locator(self.QR_SELECTOR).is_visible()

    async def _wait_for_authentication(self, page):
        timeout = int(cfg.get("weread.login_timeout", 300) or 300)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self._page_is_authenticated(page):
                return
            await asyncio.sleep(0.5)
        raise asyncio.TimeoutError("微信读书扫码登录超时")

    @staticmethod
    def _has_auth_cookies(cookies):
        names = {
            item.get("name")
            for item in (cookies or [])
            if item.get("value")
        }
        return {"wr_localvid", "wr_ql"}.issubset(names)

    def status(self):
        stored = self.store.has_auth_cookie()
        login_status = self.state == "confirmed" or (
            self.state == "idle" and stored
        )
        return {
            "state": self.state,
            "login_status": login_status,
            "qr_code": (
                f"/static/weread_qrcode.png?t={self.updated_at or int(time.time())}"
                if self.state == "waiting_scan" and os.path.exists(self.qr_path)
                else None
            ),
            "error": self.error,
            "updated_at": self.updated_at,
        }

    async def unbind(self):
        async with self._lock:
            if self.task is not None and not self.task.done():
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass
            self.task = None
            self.store.clear()
            self._remove_qr()
            self.state = "idle"
            self.error = None
            self.updated_at = int(time.time())
        return self.status()

    def _remove_qr(self):
        try:
            os.remove(self.qr_path)
        except FileNotFoundError:
            pass


WEREAD_LOGIN = WereadLoginService()
