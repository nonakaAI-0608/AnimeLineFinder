# -*- coding: utf-8 -*-
"""Local litagin/anime-whisper runner using the original Hugging Face safetensors.

This is deliberately a separate process / Python environment from the Tk UI.
Do NOT pass an initial_prompt to this particular model: its author warns that
prompting substantially degrades recognition and induces hallucinations.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

from core import Cue, split_cues, suspicious_transcript_reason

MODEL_ID = 'litagin/anime-whisper'
SAMPLE_RATE = 16000
WINDOW_SECONDS = 25.0
OVERLAP_SECONDS = 1.5


def format_srt_time(seconds: float) -> str:
    msec = max(0, int(round(seconds * 1000)))
    h, remainder = divmod(msec, 3600000)
    m, remainder = divmod(remainder, 60000)
    s, ms = divmod(remainder, 1000)
    return f'{h:02}:{m:02}:{s:02},{ms:03}'


def write_srt(output: Path, cues: list[tuple[float, float, str]]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    contents = []
    for i, (a, b, text) in enumerate(cues, 1):
        contents.append(f'{i}\n{format_srt_time(a)} --> {format_srt_time(b)}\n{text}')
    output.write_text('\n\n'.join(contents) + '\n', encoding='utf-8-sig')


def windows(duration: float, chunk: float = WINDOW_SECONDS, overlap: float = OVERLAP_SECONDS):
    """Return (owned_start, owned_end, audio_start, audio_end) in seconds.

    Each inference input is no longer than ~28 seconds (Whisper 30s limit).
    The ownership boundary determines which overlapping transcript to keep.
    """
    if duration <= 0:
        return
    n = 0
    while n * chunk < duration:
        owned_a = n * chunk
        owned_b = min(duration, owned_a + chunk)
        audio_a = max(0., owned_a - overlap)
        audio_b = min(duration, owned_b + overlap)
        yield owned_a, owned_b, audio_a, audio_b
        n += 1


def group_word_chunks(chunks: list[dict], max_chars: int = 24, max_seconds: float = 4.0,
                      gap_seconds: float = 0.55) -> list[dict]:
    """Group Whisper word-timestamp tokens into short spoken phrases."""
    output = []
    text = ''
    a = b = None
    for item in chunks:
        token = str(item.get('text', ''))
        if not token.strip():
            continue
        times = item.get('timestamp') or (None, None)
        try:
            wa = float(times[0])
            wb = float(times[1])
        except (TypeError, ValueError, IndexError):
            if text:
                output.append({'text': text, 'timestamp': (a, b)})
                text, a, b = '', None, None
            output.append(item)
            continue
        if wb < wa:
            wb = wa
        if text and (wa - b >= gap_seconds or
                     len(text.strip()) + len(token.strip()) > max_chars or
                     wb - a > max_seconds):
            output.append({'text': text.strip(), 'timestamp': (a, b)})
            text, a, b = '', None, None
        if a is None:
            a = wa
        text += token
        b = wb
        if text.rstrip().endswith(('。', '！', '？', '!', '?', '」', '』')):
            output.append({'text': text.strip(), 'timestamp': (a, b)})
            text, a, b = '', None, None
    if text:
        output.append({'text': text.strip(), 'timestamp': (a, b)})
    return output


def extract_cues(result: dict, audio_a: float, audio_b: float,
                 owned_a: float, owned_b: float, max_chars: int = 24,
                 max_seconds: float = 4.0, word_timestamps: bool = False) -> list[tuple[float, float, str]]:
    """Convert HF Whisper timestamps to absolute SRT cue times.

    When the model does not provide timestamps, mark the window as a single cue
    rather than pretend to have accurate sentence-level timing.
    """
    duration = max(0., audio_b - audio_a)
    chunks = result.get('chunks') or []
    if word_timestamps:
        chunks = group_word_chunks(chunks, max_chars, max_seconds)
    if not chunks:
        chunks = [{'text': result.get('text', ''), 'timestamp': (0, duration)}]
    cues = []
    for chunk in chunks:
        spoken = ' '.join(str(chunk.get('text') or '').split())
        if not spoken:
            continue
        times = chunk.get('timestamp') or (None, None)
        try:
            start = float(times[0]) if times[0] is not None else 0.0
            end = float(times[1]) if times[1] is not None else duration
        except (ValueError, TypeError, IndexError):
            start, end = 0., duration
        start = max(0., min(start, duration))
        end = max(start, min(end, duration))
        abs_a, abs_b = audio_a + start, audio_a + end
        mid = (abs_a + abs_b) / 2.0
        if owned_a <= mid < owned_b or (abs_b == owned_b and mid <= owned_b):
            if abs_b <= abs_a:
                abs_b = min(audio_b, abs_a + 0.5)
            cues.append((round(abs_a, 3), round(abs_b, 3), spoken))
    shortened = split_cues([Cue(a, b, t) for a, b, t in cues], max_chars, max_seconds)
    return [(c.start, c.end, c.text) for c in shortened]


def find_ffmpeg(specified: str) -> str:
    if specified:
        exe = Path(specified)
        if not exe.is_file():
            raise RuntimeError(f'指定されたFFmpegが存在しません：{specified}')
        return str(exe)
    found = shutil.which('ffmpeg')
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError('FFmpegを利用できません。インストール処理を再実行するか、設定画面でFFmpegを指定してください。') from exc


def prepare_audio(video: Path, wav_path: Path, ffmpeg: str) -> None:
    command = [ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
               '-i', str(video), '-map', '0:a:0', '-vn', '-sn', '-dn',
               '-ac', '1', '-ar', str(SAMPLE_RATE), '-c:a', 'pcm_s16le', str(wav_path)]
    print('音声を一時WAVへ変換中（BGM除去なし）', flush=True)
    run = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if run.returncode or not wav_path.is_file() or not wav_path.stat().st_size:
        stderr = run.stderr.decode('utf-8', errors='replace')[-1000:]
        raise RuntimeError(f'音声抽出に失敗しました：{stderr}')


def load_audio(wav_path: Path):
    import numpy as np
    with wave.open(str(wav_path), 'rb') as reader:
        if reader.getnchannels() != 1 or reader.getframerate() != SAMPLE_RATE or reader.getsampwidth() != 2:
            raise RuntimeError('変換後WAVが16kHz・mono・16bit PCMではありません。')
        frames = reader.readframes(reader.getnframes())
    return np.frombuffer(frames, dtype='<i2').astype(np.float32) / 32768.0


def get_pipeline(cache_dir: Path):
    import torch
    from transformers import pipeline
    device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
    dtype = torch.float16 if device.startswith('cuda') else torch.float32
    if device == 'cpu':
        print('注意：CUDAを利用できないためCPUで動作します。非常に時間がかかる場合があります。', flush=True)
    else:
        print('GPU: ' + torch.cuda.get_device_name(0), flush=True)
    print(f'モデル: {MODEL_ID}（公式オリジナル / Transformers）', flush=True)
    print('初回は約3GBのモデルデータをダウンロードします。', flush=True)
    # No initial_prompt. Avoid trust_remote_code.
    pipe = pipeline('automatic-speech-recognition', model=MODEL_ID,
                    device=device, dtype=dtype,
                    batch_size=1,
                    model_kwargs={'cache_dir': str(cache_dir), 'use_safetensors': True})
    return pipe


def supports_word_timestamps(pipe) -> bool:
    """Only request DTW word alignment when the model declares valid heads.

    Anime Whisper has 2 decoder layers, but its inherited generation config
    references layers 7..25. Requesting word timestamps in Transformers then
    raises IndexError inside _extract_token_timestamps. Segment timestamps do
    not require these attention heads and work with the original weights.
    """
    model = getattr(pipe, 'model', None)
    config = getattr(model, 'config', None)
    gen_config = getattr(model, 'generation_config', None)
    n_layers = getattr(config, 'decoder_layers', None)
    n_heads = getattr(config, 'decoder_attention_heads', None)
    alignment_heads = getattr(gen_config, 'alignment_heads', None)
    if not isinstance(n_layers, int) or not isinstance(n_heads, int) or not alignment_heads:
        return False
    try:
        return all(0 <= int(layer) < n_layers and 0 <= int(head) < n_heads
                   for layer, head in alignment_heads)
    except (TypeError, ValueError):
        return False



def pack_speech_spans(spans: list[tuple[float, float]], duration: float,
                      max_seconds: float = 25.0, join_gap: float = 0.65) -> list[tuple[float, float]]:
    """Merge neighboring VAD speech regions while keeping every ASR input short.

    Coordinates always refer to the ORIGINAL unmodified recording. No audio
    concatenation, time compression, or synthetic speech timestamps.
    """
    clean = []
    for start, end in sorted(spans):
        a, b = max(0., float(start)), min(float(duration), float(end))
        if b <= a:
            continue
        while b - a > max_seconds + 1e-6:
            clean.append((a, a + max_seconds))
            a += max_seconds
        if b > a:
            clean.append((a, b))
    merged = []
    for a, b in clean:
        if merged and a - merged[-1][1] <= join_gap and b - merged[-1][0] <= max_seconds:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return [(round(a, 4), round(b, 4)) for a, b in merged]


def detect_speech_spans(audio, threshold: float = 0.35) -> list[tuple[float, float]]:
    """Silero VAD on 16 kHz mono float samples. Does not remove BGM or vocals.

    The packaged CPU JIT model does not need torch hub, ffmpeg extras,
    torchaudio, network downloads, or a second GPU model.
    """
    try:
        from silero_vad import load_silero_vad, get_speech_timestamps
    except ImportError as exc:
        raise RuntimeError('Silero VADが未導入です。設定タブの「VADだけ追加導入」を押してください。'
                           'VADを使わずに実行する場合は、動画タブのVADをOFFにしてください。') from exc
    import torch
    import numpy as np
    print(f'音声区間を検出中（Silero VAD、感度しきい値={threshold:.2f}、BGM除去なし）', flush=True)
    vad_model = load_silero_vad(onnx=False)
    with torch.inference_mode():
        samples = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32))
        timestamps = get_speech_timestamps(
            samples, vad_model, sampling_rate=SAMPLE_RATE, threshold=threshold,
            min_speech_duration_ms=120, min_silence_duration_ms=200,
            speech_pad_ms=220, max_speech_duration_s=23.0)
    return [(float(t['start']) / SAMPLE_RATE, float(t['end']) / SAMPLE_RATE)
            for t in timestamps]


def transcribe(video: Path, output: Path, cache_dir: Path, ffmpeg_arg: str, keep_wav: bool = False,
               max_chars: int = 24, max_seconds: float = 4.0, no_repeat_ngram_size: int = 5,
               vad_enabled: bool = True, vad_threshold: float = 0.35) -> None:
    import tempfile
    cache_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg(ffmpeg_arg)
    with tempfile.TemporaryDirectory(prefix='anime_whisper_') as temp:
        wav = Path(temp) / 'audio.wav'
        prepare_audio(video, wav, ffmpeg)
        audio = load_audio(wav)
        duration = len(audio) / SAMPLE_RATE
        print(f'音声: {duration:.1f}秒', flush=True)
        if vad_enabled:
            raw_spans = detect_speech_spans(audio, vad_threshold)
            speech_spans = pack_speech_spans(raw_spans, duration)
            audible = sum(b - a for a, b in speech_spans)
            print(f'VAD検出: {len(raw_spans)}発話候補 → {len(speech_spans)}認識区間'
                  f'（合計{audible:.1f}秒 / 全{duration:.1f}秒）', flush=True)
            if not speech_spans:
                raise RuntimeError('VADで発話が検出されませんでした。'
                                   'VAD感度を「0.25」に下げるか、OFFにしてやり直してください。')
            # Never feed long non-dialogue stretches to Whisper. Keeping the
            # source offsets preserves seek timestamps for MPC playback.
            planned = [(a, b, a, b) for a, b in speech_spans]
        else:
            print('VAD無効：従来の固定長区間で文字起こしします。', flush=True)
            planned = list(windows(duration))
        # VAD runs before loading the large ASR model: installation/detection
        # errors fail fast and do not waste several GB of GPU model loading.
        pipe = get_pipeline(cache_dir)
        cues = []
        use_word_timestamps = supports_word_timestamps(pipe)
        if not use_word_timestamps:
            print('モデルの単語時刻設定が非対応のため、通常の字幕時刻で認識します。'
                  '長いセリフの分割時刻は推定値になります。', flush=True)
        for i, (own_a, own_b, audio_a, audio_b) in enumerate(planned, 1):
            samples = audio[round(audio_a * SAMPLE_RATE):round(audio_b * SAMPLE_RATE)]
            # The model's author recommends these generation settings.
            generation = {'language': 'Japanese', 'task': 'transcribe',
                          'no_repeat_ngram_size': no_repeat_ngram_size, 'repetition_penalty': 1.0}
            # Word timestamps are optional. Most importantly, do not request
            # them when alignment_heads references missing decoder layers.
            try:
                result = pipe(samples, return_timestamps=('word' if use_word_timestamps else True),
                              generate_kwargs=generation)
            except (IndexError, RuntimeError, ValueError, TypeError) as exc:
                if not use_word_timestamps:
                    raise
                print(f'単語時刻の取得に失敗したため通常時刻へ切替：{type(exc).__name__}: {exc}', flush=True)
                use_word_timestamps = False
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass
                result = pipe(samples, return_timestamps=True, generate_kwargs=generation)
            # This model is prone to getting stuck in repeating syllables over
            # music or non-dialogue audio. The author's recommended 5-10 ngram
            # guard is applied above. Try a stronger second pass only when the
            # *first* pass clearly repeats. Never silently delete its captions.
            text_before = str(result.get('text') or ''.join(c.get('text', '') for c in result.get('chunks', [])))
            warning = suspicious_transcript_reason(text_before)
            if warning and no_repeat_ngram_size != 0:
                print(f'要確認 {audio_a:.1f}-{audio_b:.1f}秒：{warning}。設定を強めて再認識を試行します。', flush=True)
                stricter = dict(generation, no_repeat_ngram_size=3, repetition_penalty=1.1)
                try:
                    alternate = pipe(samples, return_timestamps=('word' if use_word_timestamps else True),
                                     generate_kwargs=stricter)
                    text_after = str(alternate.get('text') or ''.join(
                        c.get('text', '') for c in alternate.get('chunks', [])))
                    if text_after.strip() and not suspicious_transcript_reason(text_after):
                        result = alternate
                        print('再認識で明らかな反復が解消されました。', flush=True)
                    else:
                        print('再認識後も疑わしいため元の結果を保持します。検索画面で確認できます。', flush=True)
                except Exception as e:
                    print(f'再認識は失敗しましたが、最初の結果を保存します：{type(e).__name__}: {e}', flush=True)
            elif warning:
                print(f'要確認 {audio_a:.1f}-{audio_b:.1f}秒：{warning}（繰り返し抑制は無効）', flush=True)
            cues.extend(extract_cues(result, audio_a, audio_b, own_a, own_b,
                                     max_chars, max_seconds, use_word_timestamps))
            percent = round(100 * i / len(planned))
            print(f'PROGRESS:{percent}', flush=True)
            print(f'{i}/{len(planned)} 区間認識完了・現在{len(cues)}字幕', flush=True)
        if not cues:
            raise RuntimeError('文字起こし結果が空でした。音声やモデルの状態を確認してください。')
        cues.sort(key=lambda v: (v[0], v[1]))
        write_srt(output, cues)
        print(f'SRT出力完了: {len(cues)}行 -> {output}', flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Anime Whisper local transcription (original model)')
    parser.add_argument('--input', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--cache-dir', type=Path, required=True)
    parser.add_argument('--ffmpeg', default='')
    parser.add_argument('--max-chars', type=int, default=24)
    parser.add_argument('--max-seconds', type=float, default=4.0)
    parser.add_argument('--no-repeat-ngram-size', type=int, choices=(0, 5, 10), default=5)
    parser.add_argument('--download-only', action='store_true')
    parser.add_argument('--vad', choices=('on', 'off'), default='on')
    parser.add_argument('--vad-threshold', type=float, default=0.35)
    args = parser.parse_args(argv)
    if not (0.15 <= args.vad_threshold <= 0.85):
        parser.error('--vad-threshold must be between 0.15 and 0.85')
    os.environ.setdefault('HF_HOME', str(args.cache_dir))
    if not args.download_only and (not args.input or not args.output):
        parser.error('--input and --output are required')
    try:
        if args.download_only:
            args.cache_dir.mkdir(parents=True, exist_ok=True)
            get_pipeline(args.cache_dir)
            print('モデルの準備が完了しました。', flush=True)
        else:
            if not args.input.is_file():
                raise RuntimeError(f'動画ファイルが見つかりません：{args.input}')
            transcribe(args.input, args.output, args.cache_dir, args.ffmpeg,
                       max_chars=args.max_chars, max_seconds=args.max_seconds,
                       no_repeat_ngram_size=args.no_repeat_ngram_size,
                       vad_enabled=(args.vad == 'on'), vad_threshold=args.vad_threshold)
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
