"""Local keyword suggestions and auditable note-sample analysis; no invented platform metrics."""
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from analysis_core import heat_summary, score, extract_phrases

ROOT = Path(__file__).resolve().parent
CATALOG = json.loads((ROOT / 'catalog.json').read_text(encoding='utf-8'))
ROUTES = json.loads((ROOT / 'destinations.json').read_text(encoding='utf-8'))
DESTINATIONS = ROUTES['destinations']
CN = timezone(timedelta(hours=8))


def date_window(now=None):
    today = (now or datetime.now(timezone.utc)).astimezone(CN).date()
    return {'start': (today - timedelta(days=30)).isoformat(), 'end': (today - timedelta(days=1)).isoformat()}


def normalize(value):
    return re.sub(r'\s+', '', str(value or '')).lower()


def select_destinations(region):
    tokens = [normalize(x) for x in re.split(r'[,，、;/；]+', region) if x.strip()]
    if not tokens:
        return DESTINATIONS
    matched = []
    for token in tokens:
        found = [d for d in DESTINATIONS if token in [normalize(v) for v in [d['code'], d['name'], d['country'], d['region'], *d['aliases']]]]
        if token in ('全球', '不限'):
            found = DESTINATIONS
        if not found:
            raise ValueError(f'“{token}”未匹配已核验国泰航点；可清空地区，或选择下拉提示中的城市/地区。')
        matched.extend(found)
    return list({d['code']: d for d in matched}.values())


def validate_input(value):
    if not isinstance(value, dict):
        raise ValueError('查询条件必须是对象。')
    result = {}
    for key, default, limit in [('project', 'ao', 30), ('category', 'Q', 30), ('topic', '', 60), ('region', '', 100), ('mode', 'library', 20)]:
        v = value.get(key, default)
        if not isinstance(v, str) or len(v) > limit:
            raise ValueError(f'{key} 格式或长度不正确。')
        result[key] = v.strip()
    project = next((p for p in CATALOG['projects'] if p['id'] == result['project']), None)
    if not project:
        raise ValueError('请选择词库中的项目。')
    category = next((c for c in project['categories'] if c['id'] == result['category']), None)
    if not category:
        raise ValueError('所选词类不属于当前项目。')
    if result['mode'] not in ('library', 'import', 'online'):
        raise ValueError('不支持的数据模式。')
    result.update(projectName=project['name'], categoryName=category['name'])
    select_destinations(result['region'])
    return result, category


# These are research directions, not measured trends. Workbook wording is used when safely relevant.
PROFILES = {
    'brand': [('国泰航空机票', ['国泰机票', '国泰航空', 'cathay']), ('国泰商务舱', ['国泰商务舱']), ('国泰经济舱', ['国泰经济舱']), ('国泰行李额', ['国泰行李', '国泰托运']), ('国泰香港转机', ['国泰转机', '国泰香港中转']), ('国泰休息室', ['国泰休息室', '玉衡堂']), ('国泰里程', ['国泰里程', '亚万', '亚洲万里通'])],
    'competitor': [(b, [b]) for b in ['新加坡航空', '阿联酋航空', '卡塔尔航空', '香港航空', '长荣航空', '马来西亚航空', '全日空', '南方航空', '美联航', '达美航空']],
    'water': [('潜水', ['潜水', '浮潜']), ('冲浪', ['冲浪']), ('邮轮', ['邮轮', '游轮']), ('划船', ['划船', '皮划艇', '桨板']), ('海岛游', ['海岛', '出海', '跳岛'])],
    'family': [('亲子游', ['亲子', '带娃', '遛娃']), ('带宝宝坐飞机', ['宝宝坐飞机', '婴儿机票']), ('家庭自由行', ['全家', '家庭旅行']), ('亲子酒店', ['亲子酒店'])],
    'transfer': [('香港转机', ['香港转机', '香港中转']), ('香港机场交通', ['香港机场交通', '香港机场巴士', '香港机场快线']), ('香港机场轮渡', ['香港机场船', '海天', '蛇口', '琶洲']), ('香港机场休息室', ['香港机场休息室', '玉衡堂'])],
    'study': [('留学生机票', ['留学生机票', '学生票']), ('留学行李额', ['留学行李', '学生行李']), ('留学出发准备', ['留学准备', '留学行李清单']), ('学生机票认证', ['学生认证', '学生票证明'])],
    'meal': [('国泰飞机餐', ['国泰飞机餐', '国泰航空飞机餐']), ('飞机特殊餐', ['特殊餐', '素食餐']), ('飞机儿童餐', ['儿童餐', '宝宝飞机餐'])],
    'concert': [('演唱会旅行', ['演唱会', '追星']), ('音乐节旅行', ['音乐节']), ('看演出订机票', ['演出机票', '演唱会机票'])],
    'sport': [('徒步旅行', ['徒步']), ('马拉松旅行', ['马拉松']), ('高尔夫度假', ['高尔夫']), ('潜水旅行', ['潜水']), ('冲浪旅行', ['冲浪']), ('露营旅行', ['露营'])],
    'event': [('赛事观赛旅行', ['赛事', '观赛']), ('足球观赛', ['足球', '世界杯']), ('网球观赛', ['网球']), ('赛车观赛', ['赛车', 'f1'])],
    'summer': [('暑假旅行', ['暑假', '暑期']), ('暑假亲子游', ['暑假亲子', '暑期亲子']), ('暑假机票', ['暑假机票', '暑期机票'])],
    'return': [('回国机票', ['回国机票', '回中国机票']), ('回国香港中转', ['回国香港中转', '回国转机']), ('回国行李额', ['回国行李']), ('探亲回国', ['探亲', '回国看父母'])],
    'visit': [('探亲旅行', ['探亲', '看父母', '看爸妈', '看孩子']), ('带父母出国', ['带父母', '带爸妈', '长辈']), ('探亲机票', ['探亲机票'])],
    'local': [('上海看展', ['上海看展', '上海展览']), ('上海西岸', ['西岸']), ('上海周末游', ['上海周末', '上海一日游']), ('国泰周年庆', ['国泰周年', '国泰80'])],
    'travel': [('自由行', ['自由行', '旅行攻略', '旅游攻略']), ('周末短途游', ['周末游', '短途游']), ('旅行行程', ['行程', '几天合适']), ('机票', ['机票', '航班'])],
    'airfare': [('机票怎么买便宜', ['机票', '订票']), ('商务舱机票', ['商务舱']), ('经济舱机票', ['经济舱']), ('机票行李额', ['行李额', '托运行李']), ('机票改签', ['改签', '退票'])],
}
NON_GEO = {'brand', 'competitor', 'transfer', 'study', 'meal', 'airfare'}


def profile_for(i):
    text = i['topic'] or i['categoryName']
    if not i['topic'] and i['project'] == '80a':
        return 'local'
    if i['topic'] and ('品牌' in i['categoryName'] or '竞品' in i['categoryName']) and re.search('行李|休息室|里程|商务舱|经济舱|国泰|新加坡航空|阿联酋航空', text):
        return 'competitor' if '竞品' in i['categoryName'] else 'brand'
    mapping = [('竞品', 'competitor'), ('水上|潜水|冲浪|邮轮|划船|桨板', 'water'), ('亲子|带娃', 'family'),
               ('留学|品牌产品', 'study'), ('飞机餐', 'meal'), ('品牌', 'brand'), ('转机|中转|ITMD|香港航线', 'transfer'),
               ('演唱会|音乐节|追星', 'concert'), ('世界杯|赛事', 'event'), ('运动|徒步|高尔夫|露营', 'sport'),
               ('暑期|暑假', 'summer'), ('探亲', 'visit'), ('回国|回流', 'return'), ('西岸|本地|江浙沪', 'local'),
               ('目的地|攻略|出行场景', 'travel'), ('机票|航线|大促', 'airfare')]
    for pattern, family in mapping:
        if re.search(pattern, text, re.I):
            return family
    return 'custom' if i['topic'] else 'travel'


def candidates(i):
    family = profile_for(i)
    selected = select_destinations(i['region'])
    items = PROFILES.get(family, [(i['topic'], [i['topic']])])
    if i['topic'] and family in ('brand', 'competitor'):
        matching = [x for x in items if normalize(i['topic']) in normalize(x[0]) or any(normalize(a) in normalize(i['topic']) for a in x[1])]
        if matching:
            items = matching
    if not i['topic'] and i['project'] == '80a' and i['category'] == 'AY':
        items = [('国泰周年庆', ['国泰周年', '国泰80'])]
    if i['topic'] and family == 'water' and i['topic'] != '水上运动':
        items = [x for x in items if any(a in i['topic'] or i['topic'] in a for a in x[1])] or items
    if not i['region'] and family in NON_GEO:
        selected = [None]
    elif not i['region']:
        if i['project'] == '80a':
            selected = [d for d in selected if d['code'] == 'SHA']
        elif i['project'] == 'vancouver':
            selected = [d for d in selected if d['code'] == 'YVR']
        elif '东南亚' in i['categoryName'] or family == 'water':
            selected = [d for d in selected if d['region'] == '东南亚']
    result = []
    for seed, aliases in items:
        for d in selected:
            # Don't manufacture an association between inland cities and sea activities in offline suggestions.
            if family == 'water':
                allowed = {'潜水': {'DPS', 'HKT', 'CEB'}, '冲浪': {'DPS'}, '邮轮': {'SIN', 'HKG'}, '划船': {'SIN', 'DPS', 'HKT', 'CEB', 'HKG'}, '海岛游': {'DPS', 'HKT', 'CEB'}}
                if d and d['code'] not in allowed.get(seed, set()):
                    continue
            result.append({'hotspot': seed, 'aliases': aliases, 'destination': d, 'family': family})
    return result


def keyword_suggestions(candidate, words, notes=()):
    seed, family, d = candidate['hotspot'], candidate['family'], candidate['destination']
    place = d['name'] if d else ''
    base = seed if not place or place in seed else place + seed
    tailored = {
        '国泰行李额': ['国泰行李额多少公斤', '国泰行李额怎么买', '国泰超重行李多少钱', '国泰手提行李尺寸', '国泰托运行李规定', '国泰行李额能合并吗'],
        '国泰香港转机': ['国泰香港转机流程', '国泰香港转机行李直挂', '国泰香港转机要多久', '国泰香港转机需要重新值机吗', '国泰香港转机可以出机场吗', '国泰香港转机过夜'],
        '国泰休息室': ['国泰休息室怎么进', '国泰休息室可以带人吗', '国泰休息室哪个好', '香港机场国泰休息室', '国泰休息室开放时间', '国泰休息室餐食'],
        '国泰里程': ['国泰里程怎么兑换机票', '国泰里程怎么积累', '国泰里程有效期', '国泰里程兑换商务舱', '亚洲万里通怎么用', '国泰里程兑换攻略'],
        '留学行李额': ['留学生机票行李额', '国泰留学生行李额', '留学行李额怎么申请', '留学托运行李尺寸', '留学超重行李怎么办', '留学生行李额认证'],
        '学生机票认证': ['国泰学生机票怎么认证', '留学生机票认证材料', '学生机票认证要多久', '学生机票怎么买', '国泰学生票证明', '学生票行李额怎么申请'],
        '飞机特殊餐': ['国泰特殊餐怎么预订', '国泰特殊餐提前多久订', '国泰素食餐', '国泰无麸质飞机餐', '飞机特殊餐怎么选', '国泰特殊餐在哪预订'],
        '飞机儿童餐': ['国泰儿童餐怎么预订', '国泰儿童餐提前多久订', '国泰儿童餐怎么样', '宝宝飞机餐怎么申请', '国泰婴儿餐', '国泰儿童餐年龄'],
        '回国行李额': ['回国机票行李额', '回国托运行李规定', '回国超重行李怎么办', '回国行李直挂', '国泰回国行李额', '回国机票怎么买行李额'],
    }
    if seed in tailored:
        proposed = [place + x if place and place not in x else x for x in tailored[seed]]
    elif family == 'brand':
        proposed = [base, base + '怎么样', base + '攻略', base + '体验', base + '怎么选', base + '注意事项']
        if '机票' in seed:
            proposed = [base, '国泰机票怎么买便宜', '国泰机票哪里买', '国泰机票优惠', '国泰机票改签', '国泰机票行李额']
            if place:
                proposed = [place + x if place not in x else x for x in proposed]
    elif family == 'competitor':
        proposed = [base + '怎么样', base + '商务舱', base + '经济舱', base + '行李额', base + '飞机餐', seed + '和国泰哪个好']
    elif family == 'meal':
        proposed = [base, base + '怎么预订', base + '提前多久订', base + '怎么样', base + '推荐', base + '怎么选']
    elif family in ('airfare', 'return'):
        proposed = [base, base + '攻略', place + '机票怎么买便宜', place + '机票行李额', place + '机票改签', place + '机票提前多久买']
    else:
        proposed = [base, base + '攻略', base + '预算', base + '几天合适', base + '行李清单', base + '注意事项']
        if place and place != '香港':
            proposed += ['香港飞' + place + '机票', '经香港转机去' + place]
    bank = set(words)
    phrase_counts = Counter()
    for note in notes:
        content = note['title'] + ' ' + note.get('text', '')
        phrases = re.findall(r'#([\w\u4e00-\u9fff]{3,24})(?:\[话题\])?#?', content)
        present = set()
        for phrase in phrases:
            if normalize(phrase).startswith(normalize(base)) and not re.search(r'\d{6}|仙本那|涛岛|济州', phrase):
                present.add(phrase)
        phrase_counts.update(present)
    result = [{'text': phrase, 'kind': '笔记出现'} for phrase, count in phrase_counts.most_common(3)]
    source_added = 0
    # Only take short, directly related source terms; preserve source vs generated distinctions.
    for word in words:
        if len(word) > 26 or not normalize(word).startswith(normalize(base)):
            continue
        if re.search(r'\d{4}|世界杯|EVUS|直飞|免签|济州|仙本那|涛岛', word, re.I):
            continue
        if not d and any(city['name'] in word for city in DESTINATIONS if city['code'] != 'HKG'):
            continue
        result.append({'text': word, 'kind': '原词库'})
        source_added += 1
        if source_added == 3:
            break
    for word in proposed:
        result.append({'text': word, 'kind': '原词库' if word in bank else '拓展建议'})
    result = list({x['text']: x for x in result}.values())[:10]
    for k in result:
        if any(normalize(k['text']) in normalize(n['title'] + ' ' + n.get('text', '')) for n in notes):
            k['kind'] = '笔记出现'
    return result


def clean_notes(raw, window):
    if not isinstance(raw, list) or len(raw) > 3000:
        raise ValueError('笔记数据需为数组，一次最多 3000 条。')
    notes, seen, excluded = [], set(), Counter()
    for row in raw:
        if not isinstance(row, dict):
            excluded['format'] += 1
            continue
        url = str(row.get('url', ''))
        try:
            u = urlparse(url)
            path = re.fullmatch(r'/(?:explore|search_result|discovery/item)/([a-zA-Z0-9_-]{2,80})/?', u.path)
            valid = u.scheme == 'https' and u.hostname in ('xiaohongshu.com', 'www.xiaohongshu.com') and path and not u.username and not u.port
        except ValueError:
            valid = False
        if not valid:
            excluded['source'] += 1
            continue
        title = row.get('title', '')
        if not isinstance(title, str) or not title.strip():
            excluded['format'] += 1
            continue
        value = str(row.get('publishedAt', ''))
        try:
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                date = datetime.fromisoformat(value).date().isoformat()
            else:
                dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if dt.tzinfo is None:
                    raise ValueError('missing timezone')
                date = dt.astimezone(CN).date().isoformat()
        except (ValueError, TypeError):
            excluded['date'] += 1
            continue
        if not window['start'] <= date <= window['end']:
            excluded['date'] += 1
            continue
        key = path.group(1)
        if key in seen:
            excluded['duplicate'] += 1
            continue
        seen.add(key)
        def metric(field):
            val = row.get(field)
            return int(val) if isinstance(val, (int, float)) and not isinstance(val, bool) and 0 <= val <= 1000000000 and int(val) == val else None
        source_url = 'https://www.xiaohongshu.com' + u.path + ('?' + u.query if u.query else '')
        notes.append({'id': key, 'url': source_url, 'title': title[:300], 'text': str(row.get('text', ''))[:12000], 'publishedAt': date, 'likes': metric('likes'), 'collects': metric('collects'), 'comments': metric('comments'),
                      'datePrecision': 'approximate-day' if row.get('datePrecision') == 'approximate-day' else 'day',
                      'approximateMetrics': [k for k in ('likes', 'collects', 'comments') if k in (row.get('approximateMetrics') or [])]})
    return notes, excluded


def destination_in_text(d, text):
    aliases = [d['name'], *[a for a in d['aliases'] if a not in ('北美', '澳洲', '印尼', '台湾', '南韩')]]
    return any(normalize(a) in text for a in aliases)


def research(value, raw_notes=None, now=None):
    i, category = validate_input(value)
    window = date_window(now)
    choices = candidates(i)
    notes, excluded = clean_notes(raw_notes or [], window) if i['mode'] != 'library' else ([], Counter())
    rows, accepted = [], set()
    for choice in choices:
        evidence = []
        for n in notes:
            text = normalize(n['title'] + ' ' + n.get('text', ''))
            literal_match = any(normalize(a) in text for a in choice['aliases'])
            baggage_intent = choice['hotspot'] == '国泰行李额' and ('国泰' in text or 'cathay' in text) and any(w in text for w in ('行李', '托运', '登机箱'))
            if not (literal_match or baggage_intent):
                continue
            d = choice['destination']
            if d and not destination_in_text(d, text):
                continue
            evidence.append(n)
            accepted.add(n['id'])
        if i['mode'] != 'library' and not evidence:
            continue
        def interactions(n):
            value = score(n)
            return value if value is not None else -1
        evidence.sort(key=lambda n: (-interactions(n), n['id']))
        frequency = len(evidence) if i['mode'] != 'library' else None
        advice = '以机票购买意向承接；投放前检查实际航班、票价和落地页。'
        if choice['family'] == 'competitor':
            advice = '用于竞品比较意向；文案明确广告主为国泰，不暗示与竞品的隶属关系。'
        elif choice['family'] in ('event', 'concert', 'summer', 'local'):
            advice = '先核实活动日期与是否仍在举办；原词库中的历史活动不能直接视为当前热点。'
        elif choice['family'] == 'water':
            advice = '以已核验航空目的地承接出游需求；出海点与机场可能需另行接驳。'
        keywords = keyword_suggestions(choice, category['words'], evidence)
        attested = [k['text'] for k in keywords if k['kind'] == '笔记出现'][:3]
        summary = ('样本出现的相关用词：' + '、'.join(attested) + '。') if attested else ('相关样本匹配“' + choice['hotspot'] + '”。')
        phrases = extract_phrases(evidence, choice['aliases'])
        if phrases:
            summary += ' 高频相关话题：' + '、'.join(f'{p["text"]}（{p["count"]}篇）' for p in phrases[:3]) + '。'
        rows.append({'hotspot': choice['hotspot'], 'destination': choice['destination'], 'count': frequency, 'index': None, 'rank': None,
                     'heat': heat_summary(evidence) if evidence else None,
                     'keywords': keywords, 'advice': advice,
                     'summary': (summary + ' 来源优先展示互动字段完整且自定义评分高的笔记。') if evidence else '基于原词包和搜索意图规则生成的候选方向，尚未核验近30天热度。',
                     'sources': [{k: v for k, v in n.items() if k != 'text'} for n in evidence[:30]]})
    if i['mode'] == 'library':
        rows = rows[:10]
    else:
        rows.sort(key=lambda r: (-r['count'], r['hotspot'], (r['destination'] or {}).get('code', '')))
        rows = rows[:10]
        maximum = max((r['count'] for r in rows), default=0)
        for rank, row in enumerate(rows, 1):
            row.update(rank=rank, index=round(row['count'] / maximum * 100) if maximum else None)
        excluded['unmatched'] = len(notes) - len(accepted)
    return {'input': i, 'window': window, 'rows': rows, 'acceptedNotes': len(accepted), 'receivedNotes': len(raw_notes or []), 'excluded': dict(excluded),
            'catalogVersion': ROUTES['version'], 'sourceRange': category['range'], 'sourceWords': len(category['words']),
            'status': 'suggestions' if i['mode'] == 'library' else ('measured' if rows else 'empty'),
            'methodology': '数量为本次获取/导入笔记的去重匹配样本数，不是搜索量、曝光量或全平台笔记量。指数=本组样本数/本次最大组样本数×100。笔记可能同时属于多组。拓展建议不是已验证用户搜索词。'}
