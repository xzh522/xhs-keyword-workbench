"""Official, visible login UI only. No private APIs, cookie reads or shared profile."""
import time
from urllib.parse import urlparse

from web_sampling import collect_context


def classify_login(text, modal_visible, profile_visible, qr_visible):
    if any(x in text for x in ('访问过于频繁', '安全验证', '滑动验证', '完成验证', '网络环境存在风险')):
        return 'verification_required'
    if any(x in text for x in ('二维码已失效', '二维码已过期', '二维码过期', '二维码失效')):
        return 'expired'
    if profile_visible and not modal_visible:
        return 'authenticated'
    if modal_visible and any(x in text for x in ('扫码成功', '已扫码')):
        return 'awaiting_confirmation'
    return 'waiting_scan' if modal_visible else 'starting'


def qr_load_failed(*, elapsed, seen_qr, text):
    """A visible QR may remain valid after initial page loading has finished."""
    return elapsed > 50 and not seen_qr and '扫码成功' not in text and '确认登录' not in text


def wait_login_ui(page):
    modal = page.locator('.login-container')
    login = page.get_by_text('登录', exact=True).filter(visible=True)
    # The page may open its own modal while its shell is still rendering.
    modal.or_(login).filter(visible=True).first.wait_for(state='visible', timeout=30000)
    if not modal.is_visible():
        login.first.click(timeout=15000)
    modal.wait_for(state='visible', timeout=15000)


def launch_browser(pw, *, headless=False):
    """Use installed Edge, with a fresh temporary profile, never a personal profile."""
    return pw.chromium.launch(channel='msedge', headless=headless)


class LoginBrowser:
    def __init__(self, adapter_file):
        self.adapter_file = adapter_file
        self.pw = self.browser = self.context = self.page = None
        self.opened_at = None
        self.seen_qr = False

    def open(self):
        from playwright.sync_api import sync_playwright
        self.opened_at = time.monotonic()
        self.pw = sync_playwright().start()
        self.browser = launch_browser(self.pw)
        self.context = self.browser.new_context(viewport={'width': 1100, 'height': 850}, accept_downloads=False)
        self.page = self.context.new_page()
        self.page.set_default_timeout(10000)
        # Document lifecycle events can lag even after the official UI is visible.
        # Commit only confirms navigation; the waits below verify real login UI.
        self.page.goto('https://www.xiaohongshu.com/explore', wait_until='commit', timeout=30000)
        # Exact visible button verified on 2026-09-29; do not accept platform terms for users.
        wait_login_ui(self.page)

    def observe(self):
        page = self.page
        if urlparse(page.url).hostname != 'www.xiaohongshu.com': return {'status': 'failed'}
        modal = page.locator('.login-container')
        modal_visible = modal.count() == 1 and modal.is_visible()
        text = modal.inner_text() if modal_visible else page.locator('body').inner_text()[:3000]
        # "我" is the actual signed-in sidebar link; a disappearing QR alone is insufficient.
        profile = page.get_by_role('link', name='我', exact=True)
        profile_visible = profile.count() == 1 and profile.is_visible()
        qr = modal.locator('.qrcode')
        img = qr.locator('img.qrcode-img')
        qr_visible = modal_visible and qr.count() == 1 and qr.is_visible() and img.count() == 1 and img.is_visible()
        ready = qr_visible and img.evaluate('n => n.complete && n.naturalWidth >= 100')
        if ready: self.seen_qr = True
        state = classify_login(text, modal_visible, profile_visible, ready)
        if state == 'waiting_scan' and ready:
            # Includes official QR's white quiet zone; no base64 URL or auth token is inspected.
            return {'status': state, 'qr': qr.screenshot(type='png', timeout=8000)}
        if state in ('starting', 'waiting_scan') and qr_load_failed(
            elapsed=time.monotonic() - self.opened_at, seen_qr=self.seen_qr, text=text
        ):
            return {'status': 'failed'}
        return {'status': state}

    def collect(self, value, progress, cancelled):
        return collect_context(value, self.context, self.adapter_file, progress, cancelled)

    def close(self):
        try:
            if self.context: self.context.close()
        finally:
            try:
                if self.browser: self.browser.close()
            finally:
                if self.pw: self.pw.stop()
