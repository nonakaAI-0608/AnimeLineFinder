# -*- coding: utf-8 -*-
"""Qwen3-ASR + Qwen3-ForcedAligner runner (separate Python environment).

Speech recognition: Qwen3-ASR-1.7B (anime/galgame fine-tune by default).
Timestamps: Qwen3-ForcedAligner-0.6B aligns the recognised text to the audio,
so subtitle start/end times come from the audio instead of character-count
estimates.

Audio extraction, VAD and SRT writing are shared with anime_whisper_worker.py.
"""
from __future__ import annotations

import argparse
import os
import sys
import unicodedata
from pathlib import Path

from anime_whisper_worker import (SAMPLE_RATE, detect_speech_spans, extract_cues, find_ffmpeg, load_audio,
                                  pack_speech_spans, prepare_audio, windows, write_srt)
from core import suspicious_transcript_reason

MODELS = {
    'anime': 'jaykwok/Qwen3-ASR-1.7B-JA-Anime-Galgame',
    'base': 'Qwen/Qwen3-ASR-1.7B',
}
ALIGNER_ID = 'Qwen/Qwen3-ForcedAligner-0.6B'
BATCH = 4            # recognition spans per GPU call (8GB VRAM friendly)
MAX_SPAN = 25.0      # seconds per recognition span (aligner limit is 180s)
GAP_MIN = 2.0        # silences between VAD spans this long are recognised too


def attach_text(text: str, items) -> list[dict]:
    """Map aligner tokens back onto the original transcript.

    The aligner drops punctuation and spaces. Characters between two aligned
    tokens are appended to the previous token so 「。」「！」「？」 survive and can
    still end a subtitle phrase.
    """
    out: list[dict] = []
    cursor = 0
    for item in items:
        token = str(getattr(item, 'text', '') or '')
        try:
            a = float(getattr(item, 'start_time'))
            b = float(getattr(item, 'end_time'))
        except (TypeError, ValueError):
            continue
        span = _match(text, token, cursor) if token else None
        if span is None:
            continue  # cannot place this token; its characters stay with a neighbour
        found, end = span
        piece = text[cursor:end]  # leading gap (punctuation, skipped chars) + token
        if out and found > cursor:
            out[-1]['text'] += text[cursor:found]
            piece = text[found:end]
        out.append({'text': piece, 'timestamp': (a, max(a, b))})
        cursor = end
    if out and cursor < len(text):
        out[-1]['text'] += text[cursor:]
    return out


def _kept(ch: str) -> bool:
    """Same rule as the aligner's tokenizer: letters, numbers and apostrophe."""
    return ch == "'" or unicodedata.category(ch)[0] in 'LN'


def _match(text: str, token: str, cursor: int):
    """Find token in text from cursor, ignoring symbols the aligner removed."""
    start = text.find(token[0], cursor)
    while start >= 0:
        j, k = start, 0
        while j < len(text) and k < len(token):
            if text[j] == token[k]:
                j += 1
                k += 1
            elif not _kept(text[j]):
                j += 1
            else:
                break
        if k == len(token):
            return start, j
        start = text.find(token[0], start + 1)
    return None


def fill_gaps(audio, spans, duration: float, min_gap: float = GAP_MIN, max_len: float = MAX_SPAN,
              rate: int = SAMPLE_RATE):
    """Pieces covering the time NOT inside VAD spans, each at most max_len seconds.

    Silero VAD misses much dialogue that is mixed with BGM (anime ep.1 test: 90s
    of continuous dialogue scored 0.00). Qwen3-ASR returns no text for real
    silence, so recognising the gaps costs time but loses nothing. Long gaps are
    cut at the quietest 50 ms near each even split point, so a cut rarely falls
    inside a word.
    """
    import numpy as np
    pieces, prev = [], 0.
    for a, b in sorted(spans) + [(duration, duration)]:
        if a - prev >= min_gap:
            count = int(np.ceil((a - prev) / max_len - 1e-9))
            cuts = [prev]
            for k in range(1, count):
                target = prev + (a - prev) * k / count
                lo = max(cuts[-1] + 1., target - 2.)
                hi = min(a - 1., target + 2., cuts[-1] + max_len)
                seg = audio[int(lo * rate):int(hi * rate)]
                hop = int(.05 * rate)
                if hi - lo > .1 and len(seg) >= hop:
                    frames = seg[:len(seg) // hop * hop].reshape(-1, hop)
                    best = int(np.argmin(np.mean(frames.astype(np.float64) ** 2, axis=1)))
                    cuts.append(round(lo + (best + .5) * hop / rate, 3))
                else:
                    cuts.append(round(target, 3))
            cuts.append(a)
            pieces.extend((x, y) for x, y in zip(cuts, cuts[1:]) if y - x > .3)
        prev = max(prev, b)
    return pieces


SONG_MODEL = 'MIT/ast-finetuned-audioset-10-10-0.4593'  # AudioSet classifier (~350MB)
SONG_WIN, SONG_HOP = 5.0, 2.5   # seconds per classified window / step
SONG_MIN = 30.0                 # songs shorter than this are kept (insert songs, jingles)
SONG_BRIDGE = 5.0               # music runs closer than this are one song (intro + verse)


def song_spans(starts, speech, music, singing, win: float = SONG_WIN, hop: float = SONG_HOP,
               min_len: float = SONG_MIN, bridge: float = SONG_BRIDGE):
    """Time ranges that look like a song with vocals (OP/ED), from AudioSet scores.

    A window is "music" when the classifier hears music but little speech. Runs
    of music windows at least min_len long whose mean singing score shows vocals
    are songs. Dialogue over BGM keeps a high speech score, so it is not removed;
    instrumental BGM without dialogue may be, which loses nothing.
    """
    import numpy as np
    starts, speech, music, singing = (np.asarray(v, dtype=float) for v in (starts, speech, music, singing))
    musical = (speech < .25) & (music > .3)
    runs, i = [], 0
    while i < len(starts):
        if not musical[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(starts) and musical[j + 1]:
            j += 1
        a, b = starts[i] + (win - hop) / 2, starts[j] + (win + hop) / 2
        if runs and a - runs[-1][1] <= bridge:
            runs[-1][1], runs[-1][2] = b, runs[-1][2] + list(range(i, j + 1))
        else:
            runs.append([a, b, list(range(i, j + 1))])
        i = j + 1
    return [(round(float(a), 2), round(float(b), 2)) for a, b, idx in runs
            if b - a >= min_len and float(np.mean(singing[idx])) >= .01]


def detect_songs(audio, rate: int = SAMPLE_RATE):
    """Run the AudioSet classifier over the episode and return song_spans()."""
    import gc
    import numpy as np
    import torch
    from transformers import ASTFeatureExtractor, ASTForAudioClassification
    cuda = torch.cuda.is_available()
    device, dtype = ('cuda:0', torch.float16) if cuda else ('cpu', torch.float32)
    extractor = ASTFeatureExtractor.from_pretrained(SONG_MODEL)
    model = ASTForAudioClassification.from_pretrained(SONG_MODEL, dtype=dtype).to(device).eval()
    labels = model.config.label2id
    speech_id, music_id = labels['Speech'], labels['Music']
    sing_ids = [labels['Singing'], labels['Pop music']]
    starts = np.arange(0., max(len(audio) / rate - SONG_WIN / 2, 0.), SONG_HOP)
    rows = []
    for k in range(0, len(starts), 16):
        clips = [audio[int(a * rate):int((a + SONG_WIN) * rate)] for a in starts[k:k + 16]]
        values = extractor(clips, sampling_rate=rate, return_tensors='pt')['input_values'].to(device, dtype)
        with torch.inference_mode():
            rows.append(torch.sigmoid(model(values).logits).float().cpu().numpy())
    del model
    gc.collect()
    if cuda:
        torch.cuda.empty_cache()
    if not rows:
        return []
    p = np.concatenate(rows)
    return song_spans(starts, p[:, speech_id], p[:, music_id], p[:, sing_ids].sum(axis=1))


def inside(t: float, ranges) -> bool:
    return any(a <= t < b for a, b in ranges)


def has_words(text: str) -> bool:
    """False for results such as '…' or '♪' that contain no letters or digits."""
    return any(_kept(c) for c in text)


MIN_CUE_SECONDS = 0.6


def ensure_min_duration(cues, duration: float, minimum: float = MIN_CUE_SECONDS):
    """Aligner times for very short words can be ~0.1s; stretch the end (never the start)
    so a subtitle stays readable, without overlapping the next cue."""
    out = []
    for i, (a, b, text) in enumerate(cues):
        limit = cues[i + 1][0] if i + 1 < len(cues) else duration
        if b - a < minimum:
            b = max(b, min(a + minimum, limit))
        out.append((a, round(b, 3), text))
    return out


def load_model(model_key: str):
    import torch
    from qwen_asr import Qwen3ASRModel
    cuda = torch.cuda.is_available()
    device = 'cuda:0' if cuda else 'cpu'
    # RTX 20xx has no native BF16: FP16 on GPU, FP32 on CPU.
    dtype = torch.float16 if cuda else torch.float32
    if cuda:
        print('GPU: ' + torch.cuda.get_device_name(0), flush=True)
    else:
        print('注意：CUDAを利用できないためCPUで動作します。非常に時間がかかります。', flush=True)
    model_id = MODELS[model_key]
    print(f'モデル: {model_id} ＋ {ALIGNER_ID}', flush=True)
    print('初回は合計約5GBのモデルデータをダウンロードします。', flush=True)
    return Qwen3ASRModel.from_pretrained(
        model_id, dtype=dtype, device_map=device,
        forced_aligner=ALIGNER_ID, forced_aligner_kwargs={'dtype': dtype, 'device_map': device},
        max_inference_batch_size=BATCH, max_new_tokens=512)


def transcribe(video: Path, output: Path, ffmpeg_arg: str, model_key: str,
               max_chars: int = 24, max_seconds: float = 4.0,
               vad_enabled: bool = True, vad_threshold: float = 0.35, skip_songs: bool = True) -> None:
    import tempfile
    ffmpeg = find_ffmpeg(ffmpeg_arg)
    with tempfile.TemporaryDirectory(prefix='qwen_asr_') as temp:
        wav = Path(temp) / 'audio.wav'
        prepare_audio(video, wav, ffmpeg)
        audio = load_audio(wav)
    duration = len(audio) / SAMPLE_RATE
    print(f'音声: {duration:.1f}秒', flush=True)
    if vad_enabled:
        raw = detect_speech_spans(audio, vad_threshold)
        spans = pack_speech_spans(raw, duration, max_seconds=MAX_SPAN)
        print(f'VAD検出: {len(raw)}発話候補 → {len(spans)}認識区間'
              f'（合計{sum(b - a for a, b in spans):.1f}秒 / 全{duration:.1f}秒）', flush=True)
        gaps = fill_gaps(audio, spans, duration)
        print(f'VADが音声なしとした区間も認識します：{len(gaps)}区間（合計{sum(b - a for a, b in gaps):.1f}秒）。'
              'BGMと重なったセリフはVADで検出されないことがあるためです。', flush=True)
        planned = sorted((a, b, a, b) for a, b in spans + gaps)
    else:
        print('VAD無効：固定長区間で文字起こしします。', flush=True)
        planned = list(windows(duration))
    songs = []
    if skip_songs:
        print('OP・EDなどの歌を判定中（初回は判定用モデル約350MBをダウンロードします）', flush=True)
        try:
            songs = detect_songs(audio)
        except Exception as exc:
            print(f'注意：歌の判定に失敗したため、歌詞も含めて文字起こしします：{type(exc).__name__}: {exc}', flush=True)
        if songs:
            print('歌と判定した区間（字幕にしません）：'
                  + '、'.join(f'{a // 60:.0f}:{a % 60:04.1f}～{b // 60:.0f}:{b % 60:04.1f}' for a, b in songs), flush=True)
            planned = [p for p in planned if not any(a <= p[0] and p[1] <= b for a, b in songs)]
        else:
            print('歌と判定した区間はありませんでした。', flush=True)
    model = load_model(model_key)
    cues = []
    for start in range(0, len(planned), BATCH):
        batch = planned[start:start + BATCH]
        inputs = [(audio[round(a * SAMPLE_RATE):round(b * SAMPLE_RATE)], SAMPLE_RATE) for _oa, _ob, a, b in batch]
        try:
            results = model.transcribe(audio=inputs, language='Japanese', return_time_stamps=True)
        except Exception as exc:
            # The aligner can fail on odd text (e.g. only symbols). Keep the text
            # with span-level times instead of losing the whole batch.
            print(f'時刻合わせに失敗したため区間単位の時刻で保存：{type(exc).__name__}: {exc}', flush=True)
            results = model.transcribe(audio=inputs, language='Japanese', return_time_stamps=False)
        for (own_a, own_b, audio_a, audio_b), result in zip(batch, results):
            text = (result.text or '').strip()
            if not has_words(text):
                continue
            warning = suspicious_transcript_reason(text)
            if warning:
                print(f'要確認 {audio_a:.1f}-{audio_b:.1f}秒：{warning}', flush=True)
            items = list(getattr(result, 'time_stamps', None) or [])
            chunks = attach_text(text, items) if items else []
            payload = {'text': text, 'chunks': chunks}
            cues.extend(c for c in extract_cues(payload, audio_a, audio_b, own_a, own_b,
                                                max_chars, max_seconds, word_timestamps=bool(chunks))
                        if has_words(c[2]) and not inside((c[0] + c[1]) / 2, songs))
        done = min(len(planned), start + BATCH)
        print(f'PROGRESS:{round(100 * done / len(planned))}', flush=True)
        print(f'{done}/{len(planned)} 区間認識完了・現在{len(cues)}字幕', flush=True)
    if not cues:
        raise RuntimeError('文字起こし結果が空でした。音声やモデルの状態を確認してください。')
    cues = ensure_min_duration(sorted(cues, key=lambda v: (v[0], v[1])), duration)
    write_srt(output, cues)
    print(f'SRT出力完了: {len(cues)}行 -> {output}', flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Qwen3-ASR + ForcedAligner local transcription')
    parser.add_argument('--input', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--cache-dir', type=Path, required=True)
    parser.add_argument('--ffmpeg', default='')
    parser.add_argument('--model', choices=sorted(MODELS), default='anime')
    parser.add_argument('--max-chars', type=int, default=24)
    parser.add_argument('--max-seconds', type=float, default=4.0)
    parser.add_argument('--vad', choices=('on', 'off'), default='on')
    parser.add_argument('--vad-threshold', type=float, default=0.35)
    parser.add_argument('--songs', choices=('skip', 'keep'), default='skip',
                        help='skip: do not subtitle OP/ED and other songs')
    parser.add_argument('--download-only', action='store_true')
    args = parser.parse_args(argv)
    if not (0.15 <= args.vad_threshold <= 0.85):
        parser.error('--vad-threshold must be between 0.15 and 0.85')
    if not args.download_only and (not args.input or not args.output):
        parser.error('--input and --output are required')
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    # Must be set before transformers / huggingface_hub are imported.
    os.environ.setdefault('HF_HOME', str(args.cache_dir))
    os.environ.setdefault('HF_HUB_DISABLE_XET', '1')
    try:
        if args.download_only:
            load_model(args.model)
            from transformers import ASTFeatureExtractor, ASTForAudioClassification
            ASTFeatureExtractor.from_pretrained(SONG_MODEL)
            ASTForAudioClassification.from_pretrained(SONG_MODEL)
            print('モデルの準備が完了しました。', flush=True)
        else:
            if not args.input.is_file():
                raise RuntimeError(f'動画ファイルが見つかりません：{args.input}')
            transcribe(args.input, args.output, args.ffmpeg, args.model,
                       max_chars=args.max_chars, max_seconds=args.max_seconds,
                       vad_enabled=(args.vad == 'on'), vad_threshold=args.vad_threshold,
                       skip_songs=(args.songs == 'skip'))
        return 0
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f'ERROR: {exc}', flush=True)
        return 1


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass
    sys.exit(main())
