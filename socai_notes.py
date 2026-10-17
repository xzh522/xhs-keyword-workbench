"""Translate observed Socai 0.6 entities into the existing sample format.

Only retain research fields. Author/account metadata and CLI artifacts are not
part of a workbench result. Unknown publication dates and metrics stay unknown.
"""
from urllib.parse import urlsplit

from web_sampling import SamplingError, note_url, parse_count, publication_date


def normalize_note(payload, requested_url, query, window, now=None):
    def invalid():
        raise SamplingError('socai_invalid_result', 'Socai 返回的笔记结构或来源不匹配，已停止采样。')

    if not isinstance(payload, dict):
        invalid()
    reason = payload.get('reason')
    if reason in ('security_verification', 'challenge_required', 'rate_limited'):
        raise SamplingError('access_restricted', '小红书要求验证或限制访问，已停止采样。')
    if reason == 'login_required':
        raise SamplingError('login_required', '小红书登录已失效，请在工具内重新连接。')
    notes = payload.get('notes')
    if payload.get('ok') is False or not isinstance(notes, list) or len(notes) != 1:
        invalid()
    entity = notes[0].get('entity') if isinstance(notes[0], dict) else None
    if not isinstance(entity, dict):
        invalid()
    source = entity.get('url')
    if not isinstance(requested_url, str) or not note_url(requested_url):
        invalid()
    if not isinstance(source, str) or not note_url(source):
        invalid()
    requested_id = urlsplit(requested_url).path.rsplit('/', 1)[-1]
    if requested_id != entity.get('note_id') or requested_id != urlsplit(source).path.rsplit('/', 1)[-1]:
        invalid()
    title, content = entity.get('title'), entity.get('content')
    if not isinstance(title, str) or not title.strip() or not isinstance(content, str):
        raise SamplingError('note_unavailable', '该笔记的标题或正文不可读取，已排除本篇。')

    label = entity.get('date') if isinstance(entity.get('date'), str) else ''
    if entity.get('date_edited') is True:
        published, precision = None, 'edited'
    elif entity.get('date_edited') is False:
        published, precision = publication_date(label, now=now, window=window)
    else:
        published, precision = None, 'unknown'
    row = {'url': requested_url, 'title': title[:300], 'text': content[:12000],
           'publishedAt': published, 'datePrecision': precision, 'dateLabel': label[:120],
           'approximateMetrics': [], 'query': query}
    for destination, source in (('likes', 'likes'), ('collects', 'favorites'), ('comments', 'comments_count')):
        value = entity.get(source)
        # parse_count's historical `value or ''` treats integer zero as missing.
        value = str(value) if type(value) is int else value
        if isinstance(value, bool) or not isinstance(value, str):
            value = ''
        row[destination], approximate = parse_count(value)
        if approximate:
            row['approximateMetrics'].append(destination)
    return row
