"""Bounded ordinary-note search from visible UI, not AI or private APIs."""
import time
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from web_sampling import SamplingError, note_url


STATE_SCRIPT = r"""() => {
  const visible = el => {
    if (!el || !el.getClientRects().length || el.closest('[aria-hidden="true"]')) return false;
    const css = getComputedStyle(el);
    return css.visibility !== 'hidden' && Number(css.opacity) > .01;
  };
  const find = selector => Array.from(document.querySelectorAll(selector)).filter(visible);
  const nodes = find('section.note-item, [data-note-id], .feeds-page .note-item');
  const dialogs = find('.login-container, [role="dialog"], .captcha, .captcha-container, .verify-container, .risk-control, .error-page');
  const blockerText = dialogs.map(n => n.innerText || '').join('\n') +
    (nodes.length ? '' : (document.body?.innerText || ''));
  const input = find('.search-input textarea, #search-input-in-feeds textarea, input#search-input, input[type="search"], .search-input input')[0];
  return {
    url: location.href,
    documentState: document.readyState,
    security: /访问过于频繁|安全验证|滑动验证|完成验证|网络环境存在风险/.test(blockerText),
    login: find('.login-container, input[placeholder="输入手机号"]').length > 0,
    authenticated: find('a').some(a => (a.innerText || '').trim() === '我'),
    input: input ? input.value : null,
    nativeInput: find('input#search-input').length === 1,
    loading: find('.loading, .spinner, [class*="loading"]').length > 0,
    empty: find('.no-result, .no-results, .empty-state').some(n => /暂无搜索结果|没有找到|无结果|换个词试试|no result/i.test(n.innerText || '')),
    cards: nodes.slice(0, 40).map(n => ({
      url: Array.from(n.querySelectorAll('a[href]')).find(a => visible(a) && /\/(explore|search_result)\//.test(a.href))?.href || '',
      date: findWithin(n, '.card-bottom-wrapper .time')
    }))
  };
  function findWithin(root, selector) {
    const node = Array.from(root.querySelectorAll(selector)).find(visible);
    return node ? node.innerText : '';
  }
}"""


def search_cards(page, query, cancelled, deadline):
    """Use the loaded ordinary UI, requiring fresh cards after native submit.

    A cold/new-layout page uses the verified public ordinary URL. Neither a
    successful navigation nor a new URL alone proves the results changed.
    """
    from playwright.sync_api import Error as BrowserError
    if cancelled():
        raise SamplingError('cancelled', '已取消本轮采样。')
    remaining = min(30, deadline - time.monotonic())
    if remaining <= 0:
        raise SamplingError('page_timeout', '普通笔记搜索已超时。')
    url = 'https://www.xiaohongshu.com/search_result?' + urlencode({
        'keyword': query, 'type': '51', 'source': 'web_explore_feed'})
    previous_cards, native, refreshed = (), False, True
    try:
        before = page.evaluate(STATE_SCRIPT)
        origin = urlsplit(before.get('url', ''))
        if before.get('security'):
            raise SamplingError('access_restricted', '小红书要求验证或限制访问，已停止采样；不会自动绕过。')
        if before.get('login'):
            raise SamplingError('login_required', '小红书需要重新登录，请在工具内重新连接。')
        native = (origin.scheme == 'https' and origin.hostname == 'www.xiaohongshu.com'
                  and not origin.port and not origin.username
                  and origin.path.rstrip('/') in ('/explore', '/search_result')
                  and before.get('authenticated') and before.get('nativeInput')
                  and bool(before.get('cards'))
                  and before.get('documentState') == 'complete' and not before.get('loading'))
        if native:
            previous_cards = tuple(urlsplit(c['url']).path.rsplit('/', 1)[-1] for c in before.get('cards', []))
            refreshed = False
            control = page.locator('input#search-input:visible')
            control.fill(query, timeout=max(1, int(remaining * 1000)))
            control.press('Enter', timeout=max(1, int(min(30, deadline-time.monotonic()) * 1000)))
        else:
            page.goto(url, wait_until='commit', timeout=max(1, int(remaining * 1000)))
    except BrowserError:
        raise SamplingError('page_timeout', '小红书普通搜索页面未能加载，已停止本轮搜索。') from None
    end = min(deadline, time.monotonic() + 30)
    state, matching, stable_since, signature = {}, False, None, None
    while time.monotonic() < end:
        if cancelled():
            raise SamplingError('cancelled', '已取消本轮采样。')
        try:
            state = page.evaluate(STATE_SCRIPT)
        except BrowserError:
            state = {}
        parsed = urlsplit(state.get('url', ''))
        keywords = parse_qs(parsed.query).get('keyword', [])
        # The native ordinary UI encodes Chinese before URLSearchParams does
        # its own encoding. Decode that one extra layer, not arbitrary levels.
        keyword_matches = (len(keywords) == 1 and (keywords[0] == query or unquote(keywords[0]) == query))
        matching = (parsed.hostname == 'www.xiaohongshu.com' and parsed.scheme == 'https'
                    and not parsed.port and not parsed.username
                    and parsed.path.rstrip('/') == '/search_result'
                    and keyword_matches
                    and state.get('url') == page.url)
        if state.get('security'):
            raise SamplingError('access_restricted', '小红书要求验证或限制访问，已停止采样；不会自动绕过。')
        if state.get('login'):
            raise SamplingError('login_required', '小红书需要重新登录，请在工具内重新连接。')
        if native and matching:
            current_cards = tuple(urlsplit(c['url']).path.rsplit('/', 1)[-1] for c in state.get('cards', []))
            if not current_cards or state.get('loading') or (current_cards and current_cards != previous_cards):
                refreshed = True
        if (matching and refreshed and state.get('authenticated') and state.get('input') == query
                and not state.get('loading') and state.get('documentState') in ('interactive', 'complete')):
            seen, cards = set(), []
            for card in state.get('cards', []):
                if not note_url(card['url']):
                    continue
                identifier = urlsplit(card['url']).path.rsplit('/', 1)[-1]
                if identifier not in seen:
                    seen.add(identifier)
                    cards.append(card)
            if cards or state.get('empty'):
                current = (state['url'], tuple(c['url'] for c in cards), bool(state.get('empty')))
                if signature != current:
                    signature, stable_since = current, time.monotonic()
                elif time.monotonic() - stable_since >= .25:
                    return cards
            else:
                signature, stable_since = None, None
        else:
            signature, stable_since = None, None
        time.sleep(min(.1, max(0, end - time.monotonic())))
    if matching and state and not state.get('authenticated'):
        raise SamplingError('login_unknown', '无法核验小红书登录状态，未将可见卡片计为搜索结果。')
    if matching and state.get('loading'):
        raise SamplingError('page_timeout', '小红书结果仍在加载，已停止；不是零笔记。')
    raise SamplingError('results_unavailable', '未识别到当前关键词的普通笔记结果，已停止；不是零笔记。')
