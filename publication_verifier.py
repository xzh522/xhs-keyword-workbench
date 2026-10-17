"""Verify only visible publication metadata; note content remains Socai-owned."""
import re
import time
from urllib.parse import urlsplit

from web_sampling import SamplingError, note_url, parse_date, publication_date


DATE_STATE = r'''() => {
  const visible = e => e && e.getClientRects().length && !e.closest('[aria-hidden="true"]') &&
    getComputedStyle(e).visibility !== 'hidden' && Number(getComputedStyle(e).opacity) > .01;
  const find = s => [...document.querySelectorAll(s)].filter(visible);
  const text = s => find(s).map(e => (e.innerText || '').trim());
  const blockers = text('.login-container, [role="dialog"], .captcha, .captcha-container, .verify-container, .risk-control, .error-page').join('\n');
  return {url:location.href, state:document.readyState,
    security:/访问过于频繁|安全验证|滑动验证|完成验证|网络环境存在风险/.test(blockers),
    login:find('.login-container, input[placeholder="输入手机号"]').length > 0,
    authenticated:find('a').some(e => (e.innerText || '').trim() === '我'),
    titles:text('.note-content #detail-title'),
    dates:text('.note-content .bottom-container .date'),
    ready:find('.interaction-container .engage-bar').length === 1};
}'''


def verify_publication(page, row, window, cancelled, deadline, now=None):
    """Return a copy; never infer original publication from an omitted flag.

    Identity, title, login, readiness and a stable dedicated date are required.
    This reads no comments, note body, APIs, cookies or browser auth storage.
    """
    from playwright.sync_api import Error as BrowserError
    if cancelled():
        raise SamplingError('cancelled', '已取消发布日期核验。')
    if row.get('publishedAt') or row.get('datePrecision') == 'edited':
        return dict(row)
    end = min(deadline, time.monotonic() + 15)
    if end <= time.monotonic():
        raise SamplingError('page_timeout', '公开发布日期核验超时。')
    url = row.get('url', '')
    if not note_url(url):
        raise SamplingError('note_unavailable', '发布日期来源无法核验，未计数。')
    identifier = urlsplit(url).path.rsplit('/', 1)[-1]
    result = {**row, 'publishedAt': None, 'datePrecision': 'unknown',
              'dateSource': 'public-visible-detail', 'dateCheck': 'unknown'}
    try:
        page.goto(url, wait_until='commit', timeout=max(1, int((end-time.monotonic())*1000)))
    except BrowserError:
        raise SamplingError('page_timeout', '公开笔记日期页未能加载，未计数。') from None
    state, signature, stable_since = {}, None, None
    while time.monotonic() < end:
        if cancelled():
            raise SamplingError('cancelled', '已取消发布日期核验。')
        try:
            state = page.evaluate(DATE_STATE)
        except BrowserError:
            state = {}
        if state.get('security'):
            raise SamplingError('access_restricted', '小红书要求验证或限制访问，已停止；不会自动绕过。')
        if state.get('login'):
            raise SamplingError('login_required', '小红书需要重新登录，请在工具内重新连接。')
        current_url = state.get('url', '')
        if current_url and (not note_url(current_url) or
                            urlsplit(current_url).path.rsplit('/', 1)[-1] != identifier):
            raise SamplingError('note_unavailable', '公开日期页的笔记标识不一致，未计数。')
        if (state.get('authenticated') and state.get('ready') and state.get('state') in ('interactive', 'complete')
                and state.get('url') == page.url):
            current = (tuple(state['titles']), tuple(state['dates']))
            if current != signature:
                signature, stable_since = current, time.monotonic()
            elif time.monotonic() - stable_since >= .3:
                titles, dates = current
                compact = lambda text: re.sub(r'\s+', '', text)
                if len(titles) != 1 or compact(titles[0]) != compact(row.get('title', '')):
                    raise SamplingError('note_unavailable', '公开日期页标题无法唯一对应 Socai 笔记，未计数。')
                if len(dates) != 1 or not dates[0] or dates[0] == '刚刚':
                    return result
                label = dates[0]
                published, precision = publication_date(label, now=now, window=window)
                if precision == 'edited':
                    return {**result, 'dateLabel': label, 'datePrecision': 'edited', 'dateCheck': 'edited'}
                other = parse_date(row.get('dateLabel', ''), now=now)
                if published and other and other != published:
                    return {**result, 'dateCheck': 'conflict'}
                return {**result, 'dateLabel': label, 'publishedAt': published,
                        'datePrecision': precision, 'dateCheck': 'verified' if published else 'unknown'}
        else:
            signature, stable_since = None, None
        time.sleep(min(.1, max(0, end - time.monotonic())))
    if state and not state.get('authenticated'):
        raise SamplingError('login_unknown', '公开日期页登录状态无法核验，已停止。')
    raise SamplingError('page_timeout', '公开发布日期字段未完成加载，未计数。')
