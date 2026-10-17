"""Bounded visible-web sampling. No private APIs, cookies export, stealth or CAPTCHA bypass.

Playwright is only required when a host explicitly enables web sampling. An isolated
browser profile is owned by this app. Site-specific detail selectors must be verified
against the signed-in visible page before enabling the production collector.
"""
import importlib.util
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, urlencode

CN = timezone(timedelta(hours=8))


class SamplingError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def parse_count(value):
    value = str(value or '').strip().replace(',', '')
    match = re.fullmatch(r'(\d+(?:\.\d+)?)(万|亿|[wWkK])?(\+)?', value)
    if not match:
        return None, False
    factor = {'万': 10000, '亿': 100000000, 'w': 10000, 'k': 1000}.get((match[2] or '').lower(), 1)
    number = float(match[1]) * factor
    if number > 1000000000 or number != int(number):
        return None, False
    return int(number), bool(match[2] or match[3])


def parse_date(value, now=None):
    """Parse a dedicated publication label, never body text or comment dates."""
    value = str(value or '').strip()
    if '编辑' in value or '更新' in value:
        return None
    local_now = (now or datetime.now(timezone.utc)).astimezone(CN)
    today = local_now.date()
    try:
        full = re.match(r'^(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})', value)
        if full:
            return datetime(*map(int, full.groups())).date().isoformat()
        if value.startswith('昨天'):
            return (today - timedelta(days=1)).isoformat()
        elapsed = re.match(r'^(\d+)\s*(分钟|小时)前', value)
        if elapsed:
            delta = timedelta(minutes=int(elapsed[1])) if elapsed[2] == '分钟' else timedelta(hours=int(elapsed[1]))
            return (local_now - delta).date().isoformat()
        if value.startswith(('今天', '刚刚')):
            return today.isoformat()
        relative = re.match(r'^(\d+)\s*天前', value)
        if relative:
            return (today - timedelta(days=int(relative[1]))).isoformat()
        short = re.match(r'^(\d{1,2})[-/月](\d{1,2})(?:日|\s|$)', value)
        if short:
            month, day = map(int, short.groups())
            year = today.year - ((month, day) > (today.month, today.day))
            return datetime(year, month, day).date().isoformat()
    except (ValueError, OverflowError):
        pass
    return None


def publication_date(detail_label, card_label='', now=None, window=None):
    """Prefer an absolute search-card date over a rounded detail label.

    '7 days ago' can span two calendar dates. Reject uncertain boundary notes.
    """
    from engine import date_window
    now = now or datetime.now(timezone.utc)
    if any(x in str(detail_label) for x in ('编辑', '更新')):
        return None, 'edited'
    for label in (card_label, detail_label):
        if re.match(r'^\d{1,4}[-/年]\d{1,2}', str(label or '')):
            return parse_date(label, now), 'day'
    value = str(detail_label or card_label or '')
    date = parse_date(value, now)
    if not date:
        return None, 'unknown'
    relative = re.match(r'^(\d+)\s*(天|小时|分钟)前', value)
    if relative:
        units = {'天':'days','小时':'hours','分钟':'minutes'}
        unit = units[relative[2]]
        upper = now.astimezone(CN) - timedelta(**{unit:int(relative[1])})
        lower = upper - timedelta(**{unit:1})
        window = window or date_window(now)
        if lower.date().isoformat() < window['start'] or upper.date().isoformat() > window['end']:
            return None, 'boundary_uncertain'
        return date, 'approximate-day'
    return date, 'day'


def query_plan(value):
    from engine import candidates, validate_input, date_window
    i, _ = validate_input(value)
    choices = candidates(i)
    queries = list(dict.fromkeys(((c['destination'] or {}).get('name', '') + c['hotspot']) for c in choices))
    # Round robin by topic so a broad regional request does not sample only one topic.
    grouped = {}
    for c, query in zip(choices, [((c['destination'] or {}).get('name', '') + c['hotspot']) for c in choices]):
        grouped.setdefault(c['hotspot'], []).append(query)
    balanced = []
    for n in range(max((len(g) for g in grouped.values()), default=0)):
        for group in grouped.values():
            if n < len(group) and group[n] not in balanced:
                balanced.append(group[n])
    return {'queries': balanced[:10], 'candidateCount': len(queries), 'perQueryLimit': 5,
            'maxNotes': 50, 'window': date_window(), 'ordering': '网页默认排序（个性化、非随机样本）',
            'coverage': '最多10个检索式；每式检查首批最多40张已渲染卡片，从日期可能符合窗口的卡片中最多读5篇详情。未检索方向不是零热度。'}


def readiness(enabled, adapter_file):
    installed = importlib.util.find_spec('playwright') is not None
    return {'enabled': bool(enabled), 'installed': installed,
            'adapterVerified': adapter_ready(adapter_file),
            'message': '网页采样需部署电脑正常登录小红书；遇登录、验证或访问限制立即停止。'}


def adapter_ready(path):
    try:
        value = json.loads(Path(path).read_text())
        return bool(value.get('verifiedAt') and all(value.get('selectors', {}).get(k) for k in ('title', 'text', 'publishedAt')))
    except (OSError, ValueError, TypeError):
        return False


def check_access(page):
    # Restrict examination to visible UI. Do not read JS stores or intercepted responses.
    text = page.locator('body').inner_text(timeout=5000)
    if any(marker in text for marker in ('访问过于频繁', '安全验证', '滑动验证', '完成验证', '网络环境存在风险')):
        raise SamplingError('access_restricted', '小红书要求安全验证或限制访问，已停止采样。请在部署电脑手动处理，不会自动绕过。')
    if page.get_by_placeholder('输入手机号', exact=True).is_visible() or '登录后推荐更懂你的笔记' in text:
        raise SamplingError('login_required', '需要在部署电脑的专用采样浏览器登录小红书。请双击“登录小红书.command”，手动登录并关闭窗口后重试。')


def note_url(value):
    try:
        u = urlparse(value)
        return u.scheme == 'https' and u.hostname == 'www.xiaohongshu.com' and not u.username and not u.port and bool(re.fullmatch(r'/(?:explore|search_result)/[a-zA-Z0-9_-]{2,80}', u.path))
    except ValueError:
        return False


def visible_search_cards(page):
    """Extract only the first rendered cards and their visible date labels."""
    return page.locator('section.note-item').evaluate_all(
        """nodes => nodes.filter(n => n.getClientRects().length).slice(0, 40).map(n => ({
            url: n.querySelector('a[href*="/search_result/"]')?.href || '',
            date: n.querySelector('.card-bottom-wrapper .time')?.innerText || ''
        }))"""
    )


def log_search_failure(page):
    """Log rendered structure only, not URLs, query terms, content or credentials."""
    try:
        shape = page.locator('body').evaluate(r"""() => {
            const cards = Array.from(document.querySelectorAll('section.note-item'))
                .filter(n => n.getClientRects().length);
            const paths = cards.flatMap(n => Array.from(n.querySelectorAll('a')))
                .filter(a => a.getClientRects().length)
                .map(a => {try {const u = new URL(a.href); return u.protocol === 'https:' && u.hostname === 'www.xiaohongshu.com' ? u.pathname : ''} catch {return ''}});
            return {documentState: document.readyState, visibleCards: cards.length,
                searchNoteLinks: paths.filter(p => /^\/search_result\/[a-zA-Z0-9_-]{2,80}$/.test(p)).length,
                exploreNoteLinks: paths.filter(p => /^\/explore\/[a-zA-Z0-9_-]{2,80}$/.test(p)).length};
        }""")
    except Exception as error:
        shape = {'diagnostic': 'unavailable', 'errorClass': type(error).__name__}
    print('xhs_search_unavailable ' + json.dumps(shape), file=sys.stderr, flush=True)


def require_detail_field(locator, key, *, allow_missing=False):
    """Accept one visible field; an absent title can exclude just that note."""
    count = locator.count()
    visible = count == 1 and locator.is_visible()
    if not visible:
        if allow_missing and count == 0:
            return None
        print(f'xhs_detail_field_unmatched key={key} count={count} visible={visible}', file=sys.stderr, flush=True)
        raise SamplingError('layout_changed', '笔记详情字段无法唯一识别，已停止。请核验网页结构，不会猜测发布日期或互动数。')
    value = locator.inner_text()[:12000]
    if allow_missing and not value.strip():
        return None
    return value


def collect(value, data_dir, adapter_file, progress, cancelled):
    """Legacy entry no longer uses a shared profile."""
    raise SamplingError('login_required', '请在工具页面连接本人小红书会话后检索。')


def collect_context(value, context, adapter_file, progress, cancelled):
    """Read visible pages using exactly the caller-owned, already logged-in context."""
    from playwright.sync_api import TimeoutError as BrowserTimeout
    plan = query_plan(value)
    if value.get('_window'):
        plan['window'] = dict(value['_window'])
    if not plan['queries']:
        raise SamplingError('no_queries', '当前主题与航点没有可检索的组合，请调整条件。')
    started = time.monotonic()
    notes, seen, log, related = [], set(), [], {}
    skipped_missing_title = 0
    timed_out_details, completed_queries, stop_reason = 0, [], None
    stage, query = '采样准备', ''
    timeout_details = []
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(10000)
        if not adapter_ready(adapter_file):
            raise SamplingError('adapter_unverified', '当前小红书详情页解析尚未完成登录后的实测校验。已停止，不会把未核验内容计为真实笔记。请联系工具维护者完成页面适配。')
        adapter = json.loads(Path(adapter_file).read_text())
        selectors = adapter['selectors']
        for number, query in enumerate(plan['queries'], 1):
            if cancelled.is_set():
                raise SamplingError('cancelled', '任务已取消，没有保存不完整的排行榜。')
            if time.monotonic() - started > 600:
                stop_reason = 'time_limit'
                break
            progress(f'检索 {number}/{len(plan["queries"])}：{query}')
            # Normal public search URL observed in the site's own UI, not an API.
            try:
                stage = '搜索页导航'
                page.goto('https://www.xiaohongshu.com/search_result?' + urlencode({'keyword':query,'type':'51'}), wait_until='commit', timeout=30000)
                stage = '搜索结果等待'
                progress(f'检索 {number}/{len(plan["queries"])}：{query} · 等待搜索结果')
                deadline = time.monotonic() + 20
                cards = []
                while time.monotonic() < deadline:
                    check_access(page)
                    cards = visible_search_cards(page)
                    cards = [c for c in cards if note_url(c['url'])]
                    if cards:
                        break
                    if cancelled.wait(.5):
                        raise SamplingError('cancelled', '任务已取消，没有保存不完整结果。')
                if not cards:
                    log_search_failure(page)
                    raise SamplingError('results_unavailable', '未识别到可访问的搜索结果；可能页面变化、加载失败或需要登录。不能据此判定笔记量为零。')
                related[query] = page.locator('.query-note-wrapper .item-text').all_inner_texts()[:12]
                unique = {}
                for card in cards:
                    unique.setdefault(urlparse(card['url']).path.rsplit('/',1)[-1], card)
                eligible = [c for c in unique.values() if not parse_date(c['date']) or plan['window']['start'] <= parse_date(c['date']) <= plan['window']['end']]
                selected = eligible[:plan['perQueryLimit']]
            except BrowserTimeout:
                log_search_failure(page)
                timeout_details.append({'stage': stage, 'query': query})
                stop_reason = 'search_timeout'
                break
            detail = context.new_page()
            read_count = 0
            try:
                for card in selected:
                    url = card['url']
                    if cancelled.is_set():
                        raise SamplingError('cancelled', '任务已取消，没有保存不完整的排行榜。')
                    note_id = urlparse(url).path.rsplit('/', 1)[-1]
                    if note_id in seen:
                        continue
                    progress(f'检索 {number}/{len(plan["queries"])} · 已读取 {len(notes)} 篇，核验详情日期')
                    try:
                        stage = '笔记详情导航'
                        detail.goto(url, wait_until='commit', timeout=20000)
                        stage = '笔记访问状态检查'
                        check_access(detail)
                        # Initial skeleton displays '刚刚'; wait for actual content + interaction bar.
                        stage = '笔记正文等待'
                        detail.locator(selectors['text']).wait_for(state='visible', timeout=15000)
                        stage = '笔记互动区等待'
                        detail.locator('.interaction-container .engage-bar .chat-wrapper').wait_for(state='visible', timeout=15000)
                        stage = '笔记字段读取'
                        check_access(detail)
                        row = {'url': url, 'query': query}
                        for key in ('title', 'text', 'publishedAt', 'likes', 'collects', 'comments'):
                            selector = selectors.get(key)
                            if not selector:
                                row[key] = None
                                continue
                            locator = detail.locator(selector)
                            if key in ('title', 'text', 'publishedAt'):
                                row[key] = require_detail_field(locator, key, allow_missing=key == 'title')
                                if key == 'title' and row[key] is None:
                                    break
                                continue
                            if locator.count() != 1 or not locator.is_visible():
                                row[key] = None
                                continue
                            row[key] = locator.inner_text()[:12000]
                        if row.get('title') is None:
                            skipped_missing_title += 1
                            seen.add(note_id)
                            continue
                        row['dateLabel'] = row['publishedAt']
                        row['cardDateLabel'] = card['date']
                        row['publishedAt'], row['datePrecision'] = publication_date(row['publishedAt'], card['date'], window=plan['window'])
                        row['approximateMetrics'] = []
                        for key in ('likes', 'collects', 'comments'):
                            row[key], approx = parse_count(row[key])
                            if approx:
                                row['approximateMetrics'].append(key)
                        notes.append(row)
                        seen.add(note_id)
                        read_count += 1
                    except BrowserTimeout:
                        timed_out_details += 1
                        timeout_details.append({'stage': stage, 'query': query})
                        progress(f'{stage}超时，已跳过 {timed_out_details} 篇；已读取 {len(notes)} 篇')
                        seen.add(note_id)
                        if timed_out_details >= 3:
                            stop_reason = 'detail_timeout_limit'
                            break
                        continue
                    if cancelled.wait(2):
                        raise SamplingError('cancelled', '任务已取消，没有保存不完整的排行榜。')
            finally:
                detail.close()
            log.append({'query': query, 'scannedCards':len(unique), 'eligibleCards':len(eligible), 'selectedCards':len(selected), 'newNotesRead':read_count, 'complete': stop_reason is None})
            if stop_reason:
                break
            completed_queries.append(query)
        uncompleted_queries = plan['queries'][len(completed_queries):] if stop_reason else []
        if stop_reason and not notes:
            where = timeout_details[-1] if timeout_details else {'stage': '采样总时限', 'query': query}
            raise SamplingError('page_timeout', f'“{where["query"]}”的{where["stage"]}超时；详情超时 {timed_out_details} 篇，没有可核验笔记，未生成热度。登录仍有效时可直接重新提交；否则请重新连接。')
        if not notes and skipped_missing_title:
            raise SamplingError('layout_changed', '本次候选笔记均缺少可核验标题，没有生成热度榜；请检查小红书页面。')
        if skipped_missing_title:
            plan['coverage'] += f' 另有{skipped_missing_title}篇详情无可核验标题，已跳过且未计入样本。'
        if timed_out_details:
            plan['coverage'] += f' {timed_out_details}篇详情读取超时，未计入样本。'
        if uncompleted_queries:
            plan['coverage'] += ' 采样未完成；未完成检索式：' + '、'.join(uncompleted_queries) + '。'
        if timeout_details:
            last = timeout_details[-1]
            plan['coverage'] += f' 最近超时阶段：{last["stage"]}，检索式“{last["query"]}”。'
        return notes, {**plan, 'log': log, 'relatedSearches':related, 'adapterVerifiedAt': adapter['verifiedAt'],
                       'capturedAt': datetime.now(timezone.utc).isoformat(),
                       'complete': stop_reason is None, 'completedQueries': completed_queries,
                       'uncompletedQueries': uncompleted_queries, 'timedOutDetails': timed_out_details,
                       'timeoutDetails': timeout_details,
                       'stopReason': stop_reason,
                       'skippedDetails': {'missingTitle': skipped_missing_title},
                       'limitation': '仅搜索页前部可见样本。部分互动数经网页四舍五入；缺失/仅编辑日期不计入30天统计。'
                                     + (f' {skipped_missing_title}篇详情无可核验标题，未参与统计。' if skipped_missing_title else '')
                                     + (f' {timed_out_details}篇详情读取超时，未参与统计。' if timed_out_details else '')}
    except BrowserTimeout:
        raise SamplingError('page_timeout', f'“{query}”的{stage}超时，已停止，未生成热度。') from None
