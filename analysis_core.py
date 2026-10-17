"""Adapted from the supplied MVP's heat.py/keywords.py; scores are custom, not XHS metrics."""
import re
from collections import Counter


def score(note):
    values = [note.get(k) for k in ('likes', 'collects', 'comments')]
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in values):
        return None
    return values[0] + 2 * values[1] + 3 * values[2]


def heat_summary(notes):
    buckets = dict(low=0, medium=0, high=0, viral=0, unknown=0)
    scores = []
    for note in notes:
        value = score(note)
        bucket = 'unknown' if value is None else 'viral' if value >= 10000 else 'high' if value >= 3000 else 'medium' if value >= 500 else 'low'
        buckets[bucket] += 1
        if value is not None:
            scores.append(value)
    return {'buckets': buckets, 'maxScore': max(scores) if scores else None,
            'formula': '点赞+2×收藏+3×评论；自定义分层，非官方热度。互动数不全归为未知。'}


def extract_phrases(notes, aliases):
    """Relevant short hashtag phrases only; one vote per normalized phrase per note."""
    counts = Counter()
    for note in notes:
        terms = set(re.findall(r'#([\w\u4e00-\u9fff]{3,24})', note.get('title', '') + ' ' + note.get('text', '')))
        normalized = {re.sub(r'\s+', '', term) for term in terms}
        counts.update(term for term in normalized if any(alias in term for alias in aliases))
    return [{'text': text, 'count': count} for text, count in counts.most_common(8)]


def attach_search_suggestions(result, sampling):
    """Keep platform's visible related-search suggestions distinct from note evidence."""
    related = sampling.get('relatedSearches', {})
    for row in result['rows']:
        place = (row['destination'] or {}).get('name', '')
        query = place + row['hotspot']
        terms = [w for w in related.get(query, []) if isinstance(w, str) and 2 <= len(w) <= 35 and (not place or place in w)]
        merged = [{'text': w, 'kind': '平台相关搜索'} for w in terms[:5]] + row['keywords']
        unique = {}
        for keyword in merged:
            unique.setdefault(keyword['text'], keyword)
        row['keywords'] = list(unique.values())[:10]
