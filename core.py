"""AnimeLineFinder core: local project storage, subtitle parsing/search and Whisper CLI.
No network operations are performed by this module.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.mov', '.webm', '.m4v', '.ts', '.m2ts'}
# 初回起動時に作る「サンプル作品」の用語（登録形式の例）。自由に編集・削除できます。
SAMPLE_TERMS = [
    {'name': '山田太郎', 'reading': 'やまだ たろう', 'aliases': 'タロウ,太郎'},
    {'name': 'サンプル学園', 'reading': 'さんぷるがくえん', 'aliases': ''},
]


def data_dir() -> Path:
    override = os.environ.get('ANIME_LINE_FINDER_DATA')
    if override:
        return Path(override)
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'AnimeLineFinder'
    return Path.home() / '.local' / 'share' / 'AnimeLineFinder'


def normalize(s: str) -> str:
    return ''.join(unicodedata.normalize('NFKC', s).casefold().split())


def parse_time(s: str) -> float:
    s = s.strip().replace(',', '.')
    parts = s.split(':')
    if len(parts) not in (2, 3):
        raise ValueError(f'Invalid time: {s}')
    if len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + float(seconds)
    hours, minutes, seconds = parts
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def format_time(seconds: float, milliseconds=False) -> str:
    seconds = max(0., seconds)
    n = int(round(seconds * 1000))
    h, rem = divmod(n, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f'{h:02}:{m:02}:{s:02},{ms:03}' if milliseconds else f'{h:02}:{m:02}:{s:02}'


@dataclass
class Cue:
    start: float
    end: float
    text: str


def read_srt(path: Path) -> list[Cue]:
    raw = path.read_bytes()
    text = None
    # Decoding arbitrary Shift-JIS bytes as UTF-16 can succeed but produce garbage.
    codecs = ('utf-16', 'utf-8-sig', 'cp932') if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else ('utf-8-sig', 'cp932')
    for codec in codecs:
        try:
            text = raw.decode(codec)
            break
        except UnicodeError:
            continue
    if text is None:
        text = raw.decode('utf-8', errors='replace')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    cues: list[Cue] = []
    time_re = re.compile(r'(\d{1,2}:)?\d{1,2}:\d{1,2}[,.]\d{1,3}\s*-->\s*(\d{1,2}:)?\d{1,2}:\d{1,2}[,.]\d{1,3}')
    for block in re.split(r'\n\s*\n', text):
        lines = block.strip().splitlines()
        for n, line in enumerate(lines):
            if not time_re.fullmatch(line.strip()):
                continue
            left, right = line.strip().split('-->', 1)
            try:
                start, end = parse_time(left), parse_time(right)
            except ValueError:
                break
            spoken = ' '.join(re.sub(r'<[^>]*>', '', x).strip() for x in lines[n + 1:]).strip()
            if spoken and end >= start:
                cues.append(Cue(start, end, spoken))
            break
    return cues


def write_srt(path: Path, cues: Iterable[Cue]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks = []
    for i, cue in enumerate(cues, 1):
        blocks.append(f'{i}\n{format_time(cue.start, True)} --> {format_time(cue.end, True)}\n{cue.text}')
    path.write_text('\n\n'.join(blocks) + '\n', encoding='utf-8-sig')



def _split_text_pieces(text: str, maximum: int) -> list[str]:
    """Sentence/punctuation-first division of a transcript.

    When timestamps exist only for an entire Whisper segment, time within the
    segment cannot be recovered exactly; the caller uses proportional estimates.
    """
    text = ' '.join(text.split()).strip()
    if not text:
        return []
    maximum = max(5, int(maximum))
    chunks = []
    # Keep punctuation and closing Japanese quotes on the preceding phrase.
    sentences = re.findall(r'.*?[。！？!?]+[」』）\)]*|.+$', text)
    for sentence in sentences:
        if not sentence:
            continue
        while len(sentence) > maximum:
            left = sentence[:maximum]
            # Prefer a near-by comma or explicit space rather than cutting a word.
            cuts = [m.end() for m in re.finditer(r'[、，,；;：:\s]', left)]
            cuts = [k for k in cuts if k >= max(5, int(maximum * 0.45))]
            cut = cuts[-1] if cuts else maximum
            chunks.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            chunks.append(sentence)
    return chunks


def split_cues(cues: Iterable[Cue], max_chars: int = 24, max_seconds: float = 4.0) -> list[Cue]:
    """Split long SRT cues into short searchable phrases.

    This fallback is deliberately an approximation: without word-level timing
    exact boundaries cannot be inferred from an old SRT. A newly transcribed
    Anime Whisper clip instead uses word timestamps when supported.
    """
    max_chars = max(5, int(max_chars))
    max_seconds = max(0.5, float(max_seconds))
    result: list[Cue] = []
    for cue in cues:
        start = max(0., float(cue.start))
        end = max(start, float(cue.end))
        text = ' '.join(cue.text.split()).strip()
        if not text:
            continue
        if len(text) <= max_chars and end - start <= max_seconds:
            result.append(Cue(start, end, text))
            continue
        fragments = _split_text_pieces(text, max_chars)
        # Additional duration-based splitting, using a proportional character
        # estimate. Avoid cutting text into single characters solely because
        # the model reported an implausibly long timestamp for a short phrase.
        bounded = []
        total_length = sum(len(p) for p in fragments)
        for fragment in fragments:
            estimated_time = (end-start) * len(fragment) / total_length
            desired_parts = int(__import__('math').ceil(estimated_time / max_seconds))
            if desired_parts > 1 and len(fragment) >= 6:
                width = max(1, int(__import__('math').ceil(len(fragment) / desired_parts)))
                bounded.extend(fragment[i:i+width] for i in range(0, len(fragment), width))
            else:
                bounded.append(fragment)
        fragments = [p for p in bounded if p]
        # Compute a single cumulative mapping instead of splitting multiple
        # times; lengths only approximate speech timing.
        total_letters = sum(len(p) for p in fragments)
        elapsed = 0
        for fragment in fragments:
            a = start + (end-start)*elapsed/total_letters
            elapsed += len(fragment)
            b = start + (end-start)*elapsed/total_letters
            result.append(Cue(round(a, 3), round(max(a+0.01, b), 3), fragment))
    return result



def suspicious_transcript_reason(text: str) -> str:
    """Heuristic warning only, never proof of ASR hallucination.

    In particular an actor might intentionally repeat a syllable, so keep
    questionable lines in the SRT and make hiding optional in the UI.
    """
    plain = re.sub(r'[\s、。，．.！？!?…「」『』・ー～〜,]+', '', text)
    if len(plain) < 8:
        return ''
    if re.search(r'([^\W\d_])\1{7,}', plain, flags=re.UNICODE):
        return '同じ文字が長く連続'
    if re.search(r'(.{2,8}?)\1{3,}', plain):
        return '同じ音節が多数回連続'
    return ''


def cue_suspicion(cues: list[Cue]) -> dict[int, str]:
    """Mark suspicious lines and nearby repeated lines; do not erase data."""
    reasons = {}
    for i, cue in enumerate(cues):
        reason = suspicious_transcript_reason(cue.text)
        if reason:
            reasons[i] = reason
    # Same or almost identical phrase on 3+ adjacent short cues in <25sec.
    canon = [re.sub(r'[\s、。，．.！？!?…「」『』,]+', '', c.text) for c in cues]
    i = 0
    while i < len(cues):
        j = i+1
        while (j < len(cues) and canon[i] and canon[j] == canon[i]
               and cues[j].start - cues[i].start <= 25.0):
            j += 1
        if j - i >= 3:
            for k in range(i, j):
                reasons.setdefault(k, '近接する同じセリフが多数回連続')
        i = j
    return reasons


def cue_identity(video_path: str, start: float, text: str) -> str:
    """Stable per-line playback adjustment within a project and SRT version."""
    payload = f'{os.path.normcase(video_path)}\0{round(start * 1000)}\0{text}'
    return hashlib.sha1(payload.encode('utf-8')).hexdigest()[:20]


def playback_seek_time(cue_start: float, preroll: float, adjustment: float) -> float:
    """Positive adjustment plays later; negative adjustment plays earlier."""
    return max(0.0, float(cue_start) - max(0., float(preroll)) + float(adjustment))


def make_prompt(title: str, terms: list[dict], limit: int = 360) -> str:
    # Initial prompt is a weak bias, never a guaranteed word-level dictionary.
    prefix = f'日本語アニメ「{title}」の会話。作品中の固有名詞: '
    chunks = []
    for t in terms:
        name = t.get('name', '').strip()
        if not name:
            continue
        reading = t.get('reading', '').strip()
        chunk = f'{name}（{reading}）' if reading else name
        candidate = prefix + '、'.join(chunks + [chunk]) + '。'
        if len(candidate) > limit:
            break
        chunks.append(chunk)
    return prefix + '、'.join(chunks) + '。' if chunks else f'日本語アニメ「{title}」の会話。'


def expansions(query: str, terms: list[dict]) -> list[str]:
    """Expand a single complete query matching a registered name/reading/alias."""
    q = normalize(query)
    if not q:
        return []
    result = [q]
    for item in terms:
        variations = [item.get('name', ''), item.get('reading', '')]
        variations.extend(v.strip() for v in item.get('aliases', '').split(','))
        normalized = [normalize(v) for v in variations if normalize(v)]
        if q in normalized:
            for v in normalized:
                if v not in result:
                    result.append(v)
    return result


def search_cues(cues: list[Cue], phrase: str, terms: list[dict], window: int = 1) -> list[dict]:
    """Include adjacent SRT segments so searches can cross a segment boundary."""
    patterns = expansions(phrase, terms)
    if not patterns:
        return []
    found = []
    seen = set()
    for index, cue in enumerate(cues):
        lo, hi = max(0, index - window), min(len(cues), index + window + 1)
        merged = ''.join(normalize(c.text) for c in cues[lo:hi])
        # A match may be found in several overlapping windows; choose earliest cue
        # carrying any part of matching phrase and deduplicate nearby observations.
        for p in patterns:
            at = merged.find(p)
            if at < 0:
                continue
            pos = 0
            first = lo
            for k in range(lo, hi):
                last = pos + len(normalize(cues[k].text))
                if at < last:
                    first = k
                    break
                pos = last
            key = (first, p)
            if key not in seen:
                seen.add(key)
                found.append({'start': cues[first].start, 'end': cues[first].end,
                              'text': cues[first].text, 'context': ' / '.join(c.text for c in cues[max(0, first-1):min(len(cues), first+2)]),
                              '_first': first})
            break
    # Different patterns can find identical cue. Prevent a clutter of near-identical matches.
    unique = {}
    for f in found:
        unique.setdefault(f['_first'], f)
    return [v for _, v in sorted(unique.items())]


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def load_json(path: Path, default: Any):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return default


class Store:
    def __init__(self, root: Path | None = None):
        self.root = root or data_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.catalog_file = self.root / 'projects.json'
        self.config_file = self.root / 'settings.json'
        self.catalog = load_json(self.catalog_file, {'projects': [], 'current': ''})
        self.settings = load_json(self.config_file, {'exe': '', 'model': 'anime.ja', 'model_dir': '', 'player': '', 'ffmpeg': '', 'engine': 'anime_whisper'})
        self.settings.setdefault('engine', 'anime_whisper')
        self.project: dict | None = None
        self.project_id = ''
        if not self.catalog.get('projects'):
            self.create_project('サンプル作品', SAMPLE_TERMS)
        else:
            chosen = self.catalog.get('current') or self.catalog['projects'][0]['id']
            self.open_project(chosen)

    def project_dir(self, ident: str | None = None) -> Path:
        return self.root / 'works' / (ident or self.project_id)

    def create_project(self, name: str, terms: list[dict] | None = None):
        ident = uuid.uuid4().hex[:12]
        self.catalog['projects'].append({'id': ident, 'name': name})
        project = {'name': name, 'videos': [], 'terms': terms or []}
        save_json(self.project_dir(ident) / 'project.json', project)
        self.open_project(ident)
        return ident

    def open_project(self, ident: str):
        if not any(x['id'] == ident for x in self.catalog['projects']):
            raise ValueError('作品が存在しません')
        self.project_id = ident
        self.project = load_json(self.project_dir() / 'project.json', {'name': '作品', 'videos': [], 'terms': []})
        self.catalog['current'] = ident
        save_json(self.catalog_file, self.catalog)

    def save_project(self):
        if self.project is not None:
            save_json(self.project_dir() / 'project.json', self.project)

    def save_settings(self):
        save_json(self.config_file, self.settings)

    def rename_project(self, new_name: str):
        self.project['name'] = new_name
        for project in self.catalog['projects']:
            if project['id'] == self.project_id:
                project['name'] = new_name
        self.save_project()
        save_json(self.catalog_file, self.catalog)

    def add_paths(self, paths: Iterable[str | Path]) -> int:
        existing = {os.path.normcase(str(Path(item['path']).resolve())) for item in self.project['videos']}
        candidates = []
        for item in paths:
            p = Path(item)
            if p.is_dir():
                for parent, _dirs, files in os.walk(p):
                    for filename in files:
                        candidate = Path(parent) / filename
                        if candidate.suffix.lower() in VIDEO_EXTS:
                            candidates.append(candidate)
            elif p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                candidates.append(p)
        added = 0
        for p in sorted(candidates, key=lambda x: str(x).casefold()):
            resolved = str(p.resolve())
            key = os.path.normcase(resolved)
            if key in existing:
                continue
            self.project['videos'].append({'path': resolved, 'status': '未処理'})
            existing.add(key)
            added += 1
        if added:
            self.save_project()
        return added

    def srt_path(self, video_path: str) -> Path:
        stem = re.sub(r'[<>:"/\\|?*]', '_', Path(video_path).stem)[:60]
        ident = hashlib.sha1(os.path.normcase(video_path).encode('utf-8')).hexdigest()[:12]
        return self.project_dir() / 'srt' / f'{stem}_{ident}.srt'

    def import_srt(self, video: dict, selected_srt: Path):
        cues = read_srt(selected_srt)
        if not cues:
            raise ValueError('SRTの字幕データを読み取れませんでした')
        target = self.srt_path(video['path'])
        if selected_srt.resolve() != target.resolve():
            write_srt(target, cues)
        video['status'] = 'SRT取込済'
        self.save_project()

    def get_playback_adjustment(self, row: dict) -> float:
        key = cue_identity(row['video'], row['start'], row['text'])
        return float(self.project.get('playback_adjustments', {}).get(key, 0.))

    def set_playback_adjustment(self, row: dict, adjustment: float) -> None:
        key = cue_identity(row['video'], row['start'], row['text'])
        corrections = self.project.setdefault('playback_adjustments', {})
        value = max(-30., min(30., round(float(adjustment), 2)))
        if abs(value) < 0.001:
            corrections.pop(key, None)
        else:
            corrections[key] = value
        self.save_project()

    def results(self, phrase: str, limit: int = 5000) -> list[dict]:
        rows = []
        for video in self.project['videos']:
            srt = self.srt_path(video['path'])
            if not srt.exists():
                continue
            try:
                cues = read_srt(srt)
                warnings = cue_suspicion(cues)
                if not phrase.strip():
                    matching = [
                        {'start': cue.start, 'end': cue.end, 'text': cue.text,
                         'context': ' / '.join(x.text for x in cues[max(0, i-1):i+2]),
                         '_first': i}
                        for i, cue in enumerate(cues)
                    ]
                else:
                    matching = search_cues(cues, phrase, self.project['terms'])
                for r in matching:
                    reason = warnings.get(r.get('_first', -1), '')
                    rows.append({**r, 'video': video['path'], 'srt': str(srt),
                                 'episode': Path(video['path']).stem,
                                 'suspect_reason': reason, 'suspect': bool(reason)})
                    if limit is not None and len(rows) >= limit:
                        return rows
            except (OSError, ValueError, UnicodeError):
                continue
        return rows


def find_engine() -> str:
    roots = [Path(os.environ.get('APPDATA', '')), Path(os.environ.get('LOCALAPPDATA', ''))]
    roots += [Path(os.environ.get('PROGRAMFILES', '')), Path(os.environ.get('PROGRAMFILES(X86)', ''))]
    specific_roots = []
    for r in roots:
        if not str(r) or str(r) == '.':
            continue
        for app in ('Subtitle Edit', 'SubtitleEdit', 'subtitleedit', 'Subtitle Edit 5', 'Subtitle Edit 4'):
            specific_roots.append(r / app)
    for r in specific_roots:
        for suffix in (
                'SpeechToText/Purfview-Faster-Whisper-XXL/faster-whisper-xxl.exe',
                'Data/SpeechToText/Purfview-Faster-Whisper-XXL/faster-whisper-xxl.exe'):
            found = r / suffix
            if found.is_file():
                return str(found)
    return ''


def find_model_root(exe: str) -> Path:
    return Path(exe).parent / '_models'


def find_ffmpeg(exe: str = '') -> str:
    from_path = shutil.which('ffmpeg')
    if from_path:
        return from_path
    if exe:
        root = Path(exe).parent
        candidates = [root / 'ffmpeg.exe', root.parent.parent / 'ffmpeg' / 'ffmpeg.exe', root.parent.parent / 'ffmpeg.exe']
        for p in candidates:
            if p.is_file():
                return str(p)
    return ''


def make_command(exe: str, video: str, model: str, model_dir: str, target_dir: Path, prompt: str) -> list[str]:
    cmd = [str(exe), str(video), '--language', 'ja', '--model', model,
           '--task', 'transcribe', '--output_format', 'srt', '--output_dir', str(target_dir), '--standard', '--beep_off']
    if model_dir:
        cmd.extend(['--model_dir', model_dir])
    if prompt.strip():
        cmd.extend(['--initial_prompt', prompt])
    return cmd


def locate_generated_srt(tempdir: Path, original: str) -> Path | None:
    all_srt = list(tempdir.rglob('*.srt'))
    matching = [p for p in all_srt if p.stem == Path(original).stem]
    if len(matching) == 1:
        return matching[0]
    if len(all_srt) == 1:
        return all_srt[0]
    return None
