"""Import candidate character names from Wikipedia or Annict's official read-only API.

Only called on explicit user action. No external dependencies or web scraping of Annict.
Annict access tokens must be provided by the user at runtime and are not saved.
"""
from __future__ import annotations

import html
from html.parser import HTMLParser
import json
import re
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

WIKI_API = 'https://ja.wikipedia.org/w/api.php'
ANNICT_API = 'https://api.annict.com/v1'
USER_AGENT = 'AnimeLineFinder/1.1 (https://chatgpt.com/; desktop app, user-initiated Wikipedia queries)'


class ImportErrorUser(RuntimeError):
    """A provider/network issue safe to display to the user."""


def _get_json(url: str, params: dict, token: str = '') -> dict:
    query = urlencode(params)
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = Request(url + '?' + query, headers=headers)
    try:
        with urlopen(req, timeout=20) as res:
            raw = res.read(5_000_001)
            if len(raw) > 5_000_000:
                raise ImportErrorUser('取得結果が大きすぎるため、中断しました。')
        result = json.loads(raw.decode('utf-8'))
        if isinstance(result, dict) and 'error' in result:
            err = result['error']
            raise ImportErrorUser(str(err.get('info', err)) if isinstance(err, dict) else str(err))
        if isinstance(result, dict) and result.get('errors'):
            raise ImportErrorUser(str(result['errors'][0].get('message', 'APIエラー')))
        return result
    except HTTPError as exc:
        if exc.code in (401, 403):
            msg = '認証エラー。Annictの読み取り専用トークンを確認してください。' if token else 'サイト側からアクセスが拒否されました（403など）。'
        elif exc.code == 429:
            msg = 'アクセス制限中です。時間をおいて再試行してください。'
        else:
            msg = f'サイトへのアクセスが失敗しました（HTTP {exc.code}）。'
        raise ImportErrorUser(msg) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ImportErrorUser(f'ネットワークに接続できませんでした：{exc}') from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ImportErrorUser('サイトから返されたデータを読み取れませんでした。') from exc


def _clean_text(s: str) -> str:
    return re.sub(r'\s+', ' ', html.unescape(s).replace('\u200b', '')).strip()


def _term_from_heading(text: str) -> dict | None:
    """Conservatively parse a Japanese character heading, not character descriptions."""
    s = _clean_text(text)
    # Character heading may end with '（CV：...）' or ' - Voice Actor'.
    s = re.sub(r'\s*[（(](?:CV|声|Voice|声優)[：:].*?[）)]', '', s, flags=re.I)
    s = re.sub(r'\s*(?:\[.*?\]|\[\d+\])$', '', s)
    s = re.sub(r'^(?:[・●■\-*#]|\d+[.)])\s*', '', s)
    s = re.split(r'\s*(?:：|:| - | ― )\s*', s, maxsplit=1)[0]
    m = re.fullmatch(r'(.{1,40}?)[（(]([ぁ-ゖァ-ヶー・ ・\s]{2,50})[）)]', s)
    reading = ''
    if m:
        s, reading = m.group(1).strip(), m.group(2).strip()
    # Avoid sentence fragments, descriptions, job titles, and improbable headings.
    if not 1 <= len(s) <= 35 or '\n' in s or any(p in s for p in ('登場人物', 'キャラクター一覧', '声優一覧')):
        return None
    if re.search(r'[。！？!?、，:：]|(?:です|ます|した|について)$', s):
        return None
    if len(s.split()) >= 5:
        return None
    if not any('\u3040' <= c <= '\u30ff' or '\u3400' <= c <= '\u9fff' for c in s):
        return None
    return {'name': s, 'reading': reading, 'aliases': ''}


class CharacterHTMLParser(HTMLParser):
    """Extract definition-list dt labels in the character section of a MediaWiki page."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.section_level = None
        self.section_active = False
        self.heading_level = 0
        self.heading_buf = []
        self.in_dt = False
        self.dt_buf = []
        self.skip_depth = 0
        self.terms = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in {'h2', 'h3', 'h4'}:
            self.heading_level = int(tag[1])
            self.heading_buf = []
        elif tag == 'dt' and self.section_active:
            self.in_dt = True
            self.dt_buf = []
        elif tag in ('sup', 'style', 'script', 'rt'):
            self.skip_depth += 1
        if tag == 'br' and self.in_dt:
            self.dt_buf.append(' ')

    def handle_data(self, data):
        if self.skip_depth:
            return
        if self.heading_level:
            self.heading_buf.append(data)
        if self.in_dt:
            self.dt_buf.append(data)

    def handle_endtag(self, tag):
        if tag in ('sup', 'style', 'script', 'rt'):
            self.skip_depth = max(0, self.skip_depth - 1)
        if self.heading_level and tag == f'h{self.heading_level}':
            heading = _clean_text(''.join(self.heading_buf)).replace('[編集]', '')
            if re.search(r'^(?:登場人物|登場キャラクター|キャラクター|主要人物|主な登場人物)', heading):
                self.section_level, self.section_active = self.heading_level, True
            elif self.section_level is not None and self.heading_level <= self.section_level:
                self.section_active = False
                self.section_level = None
            self.heading_level = 0
        if tag == 'dt' and self.in_dt:
            self.in_dt = False
            t = _term_from_heading(''.join(self.dt_buf))
            if t:
                self.terms.append(t)


def _strip_wikitext(text: str) -> str:
    s = re.sub(r'<ref\b[^>]*>.*?</ref>|<ref\b[^>]*/>', '', text, flags=re.I | re.S)
    # Simple Ruby reading markup.
    s = re.sub(r'\{\{(?:Ruby|ruby|ルビ|読み仮名)\s*\|\s*([^{}|]+)\s*\|\s*([^{}|]+)\s*\}\}', r'\1（\2）', s)
    s = re.sub(r'\[\[(?:[^\]|]*\|)?([^\]|]+)\]\]', r'\1', s)
    s = re.sub(r'\'{2,}', '', s)
    s = re.sub(r'<[^>]*>', '', s)
    s = re.sub(r'\{\{[^{}]*\}\}', '', s)
    return _clean_text(s)


def parse_wikitext_terms(wikitext: str) -> list[dict]:
    terms = []
    section_level = None
    for line in wikitext.splitlines():
        m = re.match(r'^(={2,5})\s*(.*?)\s*\1\s*$', line)
        if m:
            level = len(m.group(1))
            heading = _strip_wikitext(m.group(2))
            if re.search(r'^(?:登場人物|登場キャラクター|キャラクター|主要人物|主な登場人物)', heading):
                section_level = level
            elif section_level is not None and level <= section_level:
                section_level = None
            continue
        if section_level is None:
            continue
        # Japanese Wikipedia definition list commonly marks character names with ';'.
        m = re.match(r'^\s*;\s*(.*)$', line)
        if not m:
            continue
        label = re.split(r'\s*:\s*(?=\S)', m.group(1), maxsplit=1)[0]
        t = _term_from_heading(_strip_wikitext(label))
        if t:
            terms.append(t)
    return terms


def unique_terms(terms: list[dict]) -> list[dict]:
    result = {}
    for item in terms:
        name = item['name'].strip()
        key = unicodedata.normalize('NFKC', name).casefold().replace(' ', '')
        if not key:
            continue
        if key not in result:
            result[key] = {'name': name, 'reading': item.get('reading', ''), 'aliases': item.get('aliases', '')}
        elif not result[key]['reading'] and item.get('reading'):
            result[key]['reading'] = item['reading']
    return list(result.values())


def wikipedia_search(keyword: str) -> list[dict]:
    keyword = keyword.strip()
    if not keyword:
        return []
    parts = urlsplit(keyword)
    if parts.hostname in ('ja.wikipedia.org', 'www.ja.wikipedia.org') and parts.path.startswith('/wiki/'):
        title = unquote(parts.path[len('/wiki/'):]).replace('_', ' ')
        return [{'id': title, 'title': title, 'url': 'https://ja.wikipedia.org/wiki/' + quote(title.replace(' ', '_'))}]
    doc = _get_json(WIKI_API, {'action': 'query', 'list': 'search', 'srsearch': keyword, 'srnamespace': '0', 'srlimit': '12', 'format': 'json'})
    return [{'id': it['title'], 'title': it['title'], 'url': 'https://ja.wikipedia.org/wiki/' + quote(it['title'].replace(' ', '_'))}
            for it in doc.get('query', {}).get('search', [])]


def wikipedia_terms(page_title: str) -> list[dict]:
    doc = _get_json(WIKI_API, {'action': 'parse', 'page': page_title, 'prop': 'text|wikitext', 'format': 'json'})
    if not isinstance(doc.get('parse'), dict):
        raise ImportErrorUser('Wikipediaの記事を取得できませんでした。')
    parse = doc['parse']
    parser = CharacterHTMLParser()
    parser.feed(parse.get('text', {}).get('*', ''))
    return unique_terms(parser.terms + parse_wikitext_terms(parse.get('wikitext', {}).get('*', '')))


def annict_search(keyword: str, token: str) -> list[dict]:
    if not token.strip():
        raise ImportErrorUser('Annictの読み取り専用アクセストークンが必要です。')
    doc = _get_json(ANNICT_API + '/works', {'filter_title': keyword, 'fields': 'id,title,season_name_text', 'per_page': '50'}, token.strip())
    return [{'id': it['id'], 'title': it['title'] + (f'（{it["season_name_text"]}）' if it.get('season_name_text') else ''),
             'url': f'https://annict.com/works/{it["id"]}'} for it in doc.get('works', []) if it.get('id')]


def annict_terms(work_id: int, token: str) -> list[dict]:
    terms = []
    for page in range(1, 41):
        doc = _get_json(ANNICT_API + '/casts', {'filter_work_id': work_id, 'fields': 'character.name,character.name_kana,character.nickname',
                                                   'per_page': '50', 'page': page}, token.strip())
        for cast in doc.get('casts', []):
            char = cast.get('character') or {}
            # Character name is distinct from voice actor (cast.name and person.name).
            name = char.get('name', '').strip()
            if name:
                terms.append({'name': name, 'reading': char.get('name_kana', '') or '',
                              'aliases': (char.get('nickname') or '').replace('、', ',').replace('，', ',')})
        if doc.get('next_page') is None:
            break
    return unique_terms(terms)


def merge_terms(original: list[dict], selected: list[dict]) -> tuple[list[dict], int, int]:
    """Do not overwrite manually-entered data; fill only blank readings and aliases."""
    result = [dict(x) for x in original]
    by_name = {unicodedata.normalize('NFKC', x['name']).casefold().replace(' ', ''): x for x in result if x.get('name')}
    added = updated = 0
    for item in unique_terms(selected):
        key = unicodedata.normalize('NFKC', item['name']).casefold().replace(' ', '')
        if key in by_name:
            existing = by_name[key]
            changed = False
            for field in ('reading', 'aliases'):
                if not existing.get(field) and item.get(field):
                    existing[field] = item[field]
                    changed = True
            if changed:
                updated += 1
        else:
            result.append(dict(item))
            by_name[key] = result[-1]
            added += 1
    return result, added, updated
