"""Finite ordinary-search / Socai-detail sampling with explicit exclusions."""
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlsplit

from ordinary_search import search_cards
from web_sampling import SamplingError, parse_date, query_plan


def collect_socai(value, page, reader, progress, cancelled, date_verifier=None):
    plan = query_plan(value)
    if value.get('_window'):
        plan['window'] = dict(value['_window'])
    queries = plan['queries'][:10]
    if not queries:
        raise SamplingError('no_queries', '当前词类与地区没有可检索的组合，请调整条件。')
    end = time.monotonic() + 600
    notes, seen, completed, log, timeout_details = [], set(), [], [], []
    excluded_dates, skipped = Counter(), Counter()
    missing = {k: 0 for k in ('likes', 'collects', 'comments')}
    publication_checks = []
    stop, timed_out = None, 0
    for number, query in enumerate(queries, 1):
        if cancelled.is_set():
            raise SamplingError('cancelled', '任务已取消，没有保存不完整结果。')
        if time.monotonic() >= end:
            stop = 'time_limit'
            break
        progress(f'检索 {number}/{len(queries)}：{query} · 普通笔记列表')
        try:
            cards = search_cards(page, query, cancelled.is_set, end)
        except SamplingError as error:
            if error.code not in ('page_timeout', 'results_unavailable'):
                raise
            stop = 'search_timeout' if error.code == 'page_timeout' else 'search_unavailable'
            timeout_details.append({'stage': '普通搜索结果', 'query': query})
            break
        eligible = [c for c in cards[:40] if not parse_date(c.get('date'))
                    or plan['window']['start'] <= parse_date(c['date']) <= plan['window']['end']]
        selected = eligible[:min(5, plan.get('perQueryLimit', 5))]
        read_count, accepted_count = 0, 0
        for card in selected:
            if cancelled.is_set():
                raise SamplingError('cancelled', '任务已取消，没有保存不完整结果。')
            if time.monotonic() >= end:
                stop = 'time_limit'
                break
            url = card['url']
            identifier = urlsplit(url).path.rsplit('/', 1)[-1]
            if identifier in seen:
                continue
            seen.add(identifier)
            progress(f'检索 {number}/{len(queries)} · Socai 读取正文，已核验 {len(notes)} 篇')
            detail_stage = 'Socai 笔记详情'
            try:
                row = reader.read_note(url, query, plan['window'], cancelled.is_set, end)
                if date_verifier and not row.get('publishedAt') and row.get('datePrecision') != 'edited':
                    detail_stage = '公开发布日期核验'
                    progress(f'检索 {number}/{len(queries)} · 核验公开原始发布日期，已核验 {len(notes)} 篇')
                    row = date_verifier(row, plan['window'], cancelled.is_set, end)
                    publication_checks.append({'noteId': identifier, 'source': row['dateSource'],
                                               'precision': row['datePrecision'], 'status': row['dateCheck']})
            except SamplingError as error:
                if error.code == 'note_unavailable':
                    skipped['unavailableContent'] += 1
                    continue
                if error.code != 'page_timeout':
                    raise
                timed_out += 1
                timeout_details.append({'stage': detail_stage, 'query': query})
                progress(f'详情超时，已跳过 {timed_out} 篇；已核验 {len(notes)} 篇')
                if timed_out >= 3:
                    stop = 'detail_timeout_limit'
                    break
                continue
            read_count += 1
            date = row.get('publishedAt')
            if not date:
                excluded_dates['edited' if row.get('datePrecision') == 'edited' else 'unknown'] += 1
                continue
            if not plan['window']['start'] <= date <= plan['window']['end']:
                excluded_dates['outsideWindow'] += 1
                continue
            notes.append(row)
            accepted_count += 1
            for key in missing:
                missing[key] += row.get(key) is None
            if len(notes) >= min(50, plan.get('maxNotes', 50)):
                break
        log.append({'query': query, 'scannedCards': min(40, len(cards)), 'eligibleCards': len(eligible),
                    'selectedCards': len(selected), 'newNotesRead': read_count,
                    'verifiedNotes': accepted_count, 'complete': stop is None})
        if stop:
            break
        completed.append(query)
        if len(notes) >= min(50, plan.get('maxNotes', 50)):
            if len(completed) < len(queries):
                stop = 'sample_limit'
            break
    if cancelled.is_set():
        raise SamplingError('cancelled', '任务已取消，没有保存不完整结果。')
    if not notes:
        if stop in ('search_timeout', 'detail_timeout_limit', 'time_limit'):
            raise SamplingError('page_timeout', '网页超时且没有可核验的近30天笔记，已停止，未生成热度。')
        raise SamplingError('no_verified_notes', '本轮没有原始发布日期可核验且处于近30天的笔记；编辑日期或未知日期不计数，未生成热度榜。')
    uncompleted = queries[len(completed):] if stop else []
    limitations = ['限定网页前部样本，不是平台总搜索量或曝光；互动缺失保持未知。']
    if excluded_dates:
        limitations.append('日期排除：' + '、'.join(f'{k} {v}篇' for k, v in excluded_dates.items()) + '。')
    if timed_out:
        limitations.append(f'{timed_out}篇详情超时，未计入样本。')
    if uncompleted:
        limitations.append('部分样本；未完成检索式：' + '、'.join(uncompleted) + '。')
    coverage = plan.get('coverage', '') + ' ' + ' '.join(limitations)
    return notes, {**plan, 'queries': queries, 'coverage': coverage, 'log': log,
                   'provider': 'Socai + 小红书普通笔记网页（只读样本，无API）',
                   'capturedAt': datetime.now(timezone.utc).isoformat(),
                   'complete': stop is None, 'completedQueries': completed,
                   'uncompletedQueries': uncompleted, 'stopReason': stop,
                   'timedOutDetails': timed_out, 'timeoutDetails': timeout_details,
                   'excludedDates': dict(excluded_dates), 'skippedDetails': dict(skipped),
                   'publicationChecks': publication_checks,
                   'missingMetrics': missing, 'relatedSearches': {}, 'limitation': ' '.join(limitations)}
