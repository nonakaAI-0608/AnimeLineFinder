"""AnimeLineFinder 1.0 - cross-episode Japanese anime dialogue finder (Windows-oriented)."""
from __future__ import annotations
import sys as _sys
if _sys.stdout is None or _sys.stderr is None:
    # Started with pythonw (no console): send print/progress output to a log file
    # instead of crashing on writes to a missing stdout/stderr.
    try:
        _console_log = open(__import__('os').path.join(__import__('os').path.dirname(__import__('os').path.abspath(__file__)), 'console.log'), 'w', encoding='utf-8', buffering=1)
    except OSError:
        _console_log = open(__import__('os').devnull, 'w')
    _sys.stdout = _sys.stdout or _console_log
    _sys.stderr = _sys.stderr or _console_log

import csv
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import webbrowser

from core import (Store, VIDEO_EXTS, data_dir, find_engine, find_ffmpeg, find_model_root, format_time,
                  locate_generated_srt, make_command, make_prompt, read_srt, split_cues, write_srt, playback_seek_time)
from term_import import (ImportErrorUser, annict_search, annict_terms, merge_terms,
                         wikipedia_search, wikipedia_terms)

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    Root = TkinterDnD.Tk
    HAS_DND = True
except ImportError:
    Root = tk.Tk
    HAS_DND = False

VERSION = '1.6.0'
ENGINE_ANIME = 'Anime Whisper（公式モデル・直接利用）'
ENGINE_PURFVIEW = 'Purfview（Subtitle Edit互換）'
ENGINE_QWEN = 'Qwen3-ASR ＋ ForcedAligner（高精度・正確な字幕時刻）'
ENGINE_KEYS = {ENGINE_ANIME: 'anime_whisper', ENGINE_PURFVIEW: 'purfview', ENGINE_QWEN: 'qwen3_asr'}
ENGINE_LABELS = {v: k for k, v in ENGINE_KEYS.items()}
QWEN_MODEL_LABELS = {'anime': 'アニメ・ギャルゲー追加学習版（jaykwok/Qwen3-ASR-1.7B-JA-Anime-Galgame）',
                     'base': '公式ベース版（Qwen/Qwen3-ASR-1.7B）'}


def app_folder():
    return Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent


def anime_python():
    """Reuse the installed virtualenv across app upgrades where possible."""
    exe = Path('Scripts/python.exe') if sys.platform == 'win32' else Path('bin/python')
    roots = [app_folder(), data_dir()]
    for root in roots:
        found = root / '.venv_animewhisper' / exe
        if found.is_file():
            return found
    return data_dir() / '.venv_animewhisper' / exe


def qwen_python():
    exe = Path('Scripts/python.exe') if sys.platform == 'win32' else Path('bin/python')
    for root in (app_folder(), data_dir()):
        found = root / '.venv_qwen3asr' / exe
        if found.is_file():
            return found
    return data_dir() / '.venv_qwen3asr' / exe



def open_in_default(path: Path):
    if sys.platform == 'win32':
        os.startfile(str(path))
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', str(path)])
    else:
        subprocess.Popen(['xdg-open', str(path)])


def detect_player() -> str:
    """Find installed MPC-HC/BE before other video players on Windows."""
    for name in ('mpc-hc64', 'mpc-hc', 'mpc-be64', 'mpc-be', 'mpv', 'vlc', 'ffplay'):
        found = shutil.which(name)
        if found:
            return found
    if sys.platform == 'win32':
        roots = [Path(x) for x in (os.environ.get('PROGRAMFILES'), os.environ.get('PROGRAMFILES(X86'),
                                    os.environ.get('LOCALAPPDATA')) if x]
        for root in roots:
            for relative in (
                    'K-Lite Codec Pack/MPC-HC64/mpc-hc64.exe',
                    'K-Lite Codec Pack/MPC-HC/mpc-hc.exe',
                    'K-Lite Codec Pack/MPC-BE64/mpc-be64.exe',
                    'K-Lite Codec Pack/MPC-BE/mpc-be.exe',
                    'MPC-HC/mpc-hc64.exe', 'MPC-HC/mpc-hc.exe',
                    'MPC-BE x64/mpc-be64.exe', 'MPC-BE/mpc-be.exe',
                    'Programs/MPC-HC/mpc-hc64.exe', 'Programs/MPC-BE/mpc-be64.exe',
                    'VideoLAN/VLC/vlc.exe', 'mpv/mpv.exe', 'Programs/mpv/mpv.exe',
                    'Programs/VideoLAN/VLC/vlc.exe'):
                found = root / relative
                if found.is_file():
                    return str(found)
    return ''


def player_command(player: str, video: Path, start: float) -> list[str]:
    """Build seek-compatible command line for MPC-HC/BE and other players."""
    name = Path(player).name.lower()
    if 'mpc-hc' in name or 'mpc-be' in name:
        return [player, str(video), '/start', str(round(max(0., start) * 1000)), '/play']
    if 'mpv' in name:
        return [player, f'--start={start:.3f}', str(video)]
    if 'vlc' in name:
        return [player, f'--start-time={start:.3f}', str(video)]
    if 'ffplay' in name:
        return [player, '-ss', f'{start:.3f}', '-i', str(video)]
    raise ValueError('MPC-HC・MPC-BE・mpv・VLC・ffplayの実行ファイルを指定してください。')


def popen_flags():
    return subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0


class TermDialog(tk.Toplevel):
    def __init__(self, master, initial=None):
        super().__init__(master)
        self.title('固有名詞の登録')
        self.resizable(False, False)
        self.transient(master)
        self.result = None
        initial = initial or {}
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill='both', expand=True)
        self.vars = {}
        for i, (key, label, note) in enumerate([
                ('name', '正式表記', '例：花小泉杏'),
                ('reading', '読み', '例：はなこいずみ あん'),
                ('aliases', '別表記（カンマ区切り）', '例：はなこ,ハナコ,花小泉')]):
            ttk.Label(frame, text=label).grid(row=i*2, column=0, sticky='w', pady=(4, 0))
            var = tk.StringVar(value=initial.get(key, ''))
            entry = ttk.Entry(frame, textvariable=var, width=52)
            entry.grid(row=i*2+1, column=0, sticky='ew', pady=(2, 4))
            self.vars[key] = var
            if i == 0:
                entry.focus_set()
        ttk.Label(frame, text='読みと別表記は省略できます。', foreground='#666').grid(row=6, sticky='w', pady=(6, 12))
        buttons = ttk.Frame(frame)
        buttons.grid(row=7, sticky='e')
        ttk.Button(buttons, text='キャンセル', command=self.destroy).pack(side='right', padx=(6, 0))
        ttk.Button(buttons, text='保存', command=self.confirm).pack(side='right')
        self.bind('<Return>', lambda _e: self.confirm())
        self.bind('<Escape>', lambda _e: self.destroy())
        self.grab_set()

    def confirm(self):
        item = {key: var.get().strip() for key, var in self.vars.items()}
        if not item['name']:
            messagebox.showwarning('入力不足', '正式表記を入力してください。', parent=self)
            return
        self.result = item
        self.destroy()


class ChooseArticleDialog(tk.Toplevel):
    """Explicitly choose an article/work to avoid false-name imports."""
    def __init__(self, master, items, source):
        super().__init__(master)
        self.title(f'{source}：作品の候補を選択')
        self.geometry('650x365')
        self.minsize(420, 300)
        self.transient(master)
        self.result = None
        self.items = items
        self.protocol('WM_DELETE_WINDOW', self.cancel)
        outer = ttk.Frame(self, padding=13)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='登録したい作品に対応する記事を選択してください。', font=('Meiryo UI', 10)).pack(anchor='w', pady=(0, 6))
        self.listbox = tk.Listbox(outer, font=('Meiryo UI', 10), activestyle='dotbox', exportselection=False)
        self.listbox.pack(fill='both', expand=True)
        for item in items:
            self.listbox.insert('end', item['title'])
        self.listbox.selection_set(0)
        self.listbox.activate(0)
        self.listbox.bind('<Double-1>', lambda _e: self.confirm())
        self.listbox.bind('<Return>', lambda _e: self.confirm())
        buttonrow = ttk.Frame(outer)
        buttonrow.pack(fill='x', pady=(10, 0))
        ttk.Button(buttonrow, text='掲載ページをブラウザで確認', command=self.open_url).pack(side='left')
        ttk.Button(buttonrow, text='キャンセル', command=self.cancel).pack(side='right')
        ttk.Button(buttonrow, text='この作品を選択', command=self.confirm).pack(side='right', padx=(0, 8))
        self.grab_set()
        self.focus_set()

    def chosen(self):
        ids = self.listbox.curselection()
        return self.items[ids[0]] if ids else None

    def open_url(self):
        item = self.chosen()
        if item:
            webbrowser.open(item['url'])

    def confirm(self):
        self.result = self.chosen()
        self.destroy()

    def cancel(self):
        self.destroy()


class ImportPreviewDialog(tk.Toplevel):
    """Review retrieved names; never import silently."""
    def __init__(self, master, terms, source, title, url):
        super().__init__(master)
        self.title(f'{source}：固有名詞の候補を確認')
        self.geometry('920x560')
        self.minsize(600, 350)
        self.transient(master)
        self.result = None
        self.terms = terms
        self.selected = set(range(len(terms)))
        self.url = url
        self.protocol('WM_DELETE_WINDOW', self.cancel)
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text=f'{title} ／ 取得候補：{len(terms)}件', font=('Meiryo UI', 11, 'bold')).pack(anchor='w', pady=(0, 5))
        ttk.Label(frame, text='候補をダブルクリックまたはSpaceで選択／解除できます。誤った人物名は外してください。', foreground='#555').pack(anchor='w', pady=(0, 7))
        box = ttk.Frame(frame)
        box.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(box, columns=('check', 'name', 'reading'), show='headings', selectmode='extended')
        for col, label, width in [('check', '取込', 55), ('name', '正式表記', 390), ('reading', '読み（取得できた場合）', 340)]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, stretch=(col!='check'), anchor=('center' if col=='check' else 'w'))
        self.tree.pack(side='left', fill='both', expand=True)
        sb = ttk.Scrollbar(box, orient='vertical', command=self.tree.yview)
        sb.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=sb.set)
        for i, t in enumerate(terms):
            self.tree.insert('', 'end', iid=str(i), values=('☑', t['name'], t.get('reading', '')))
        self.tree.bind('<Double-1>', self.toggle)
        self.tree.bind('<space>', self.toggle)
        bar = ttk.Frame(frame)
        bar.pack(fill='x', pady=(10, 0))
        self.count_var = tk.StringVar()
        self.refresh_count()
        ttk.Label(bar, textvariable=self.count_var).pack(side='left', padx=(0, 13))
        ttk.Button(bar, text='全選択', command=lambda: self.select_all(True)).pack(side='left', padx=(0, 5))
        ttk.Button(bar, text='全解除', command=lambda: self.select_all(False)).pack(side='left', padx=(0, 5))
        ttk.Button(bar, text='元ページを開く', command=lambda: webbrowser.open(self.url)).pack(side='left')
        ttk.Button(bar, text='キャンセル', command=self.cancel).pack(side='right')
        ttk.Button(bar, text='チェックした候補を登録', command=self.confirm).pack(side='right', padx=(0, 8))
        self.grab_set()
        self.focus_set()

    def refresh_count(self):
        self.count_var.set(f'選択：{len(self.selected)}件')

    def select_all(self, selected):
        self.selected = set(range(len(self.terms))) if selected else set()
        for i in range(len(self.terms)):
            self.tree.set(str(i), 'check', '☑' if selected else '☐')
        self.refresh_count()

    def toggle(self, event=None):
        ids = self.tree.selection()
        if event is not None and getattr(event, 'type', None) == '4':
            row = self.tree.identify_row(event.y)
            if row:
                ids = (row,)
        for iid in ids:
            idx = int(iid)
            if idx in self.selected:
                self.selected.remove(idx)
            else:
                self.selected.add(idx)
            self.tree.set(iid, 'check', '☑' if idx in self.selected else '☐')
        self.refresh_count()
        return 'break'

    def confirm(self):
        if not self.selected:
            messagebox.showinfo('候補が未選択', '登録したい候補をチェックしてください。', parent=self)
            return
        self.result = [self.terms[i] for i in sorted(self.selected)]
        self.destroy()

    def cancel(self):
        self.destroy()


class AnimeLineFinder(Root):
    def __init__(self):
        super().__init__()
        self.title('Anime Line Finder — アニメ台詞検索')
        self.geometry('1150x780')
        self.minsize(860, 610)
        self.store = Store()
        self.events = queue.Queue()
        self.worker = None
        self.proc = None
        self.proc_lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.search_rows = []
        self.search_after_id = None
        self.import_busy = False
        self.preroll_var = tk.StringVar(value=str(self.store.settings.get('play_preroll', 0.5)))
        self._ui_style()
        self._build()
        self.refresh_project_list()
        self.refresh_videos()
        self.refresh_terms()
        self.load_settings()
        self.run_search()  # Blank search initially lists every line in every episode.
        self.after(130, self.poll_events)
        self.protocol('WM_DELETE_WINDOW', self.on_close)

    def _ui_style(self):
        style = ttk.Style(self)
        if sys.platform == 'win32' and 'vista' in style.theme_names():
            style.theme_use('vista')
        elif 'clam' in style.theme_names():
            style.theme_use('clam')
        style.configure('Title.TLabel', font=('Meiryo UI', 15, 'bold'))
        style.configure('Subtitle.TLabel', font=('Meiryo UI', 9))
        style.configure('Treeview', rowheight=27)
        style.configure('TNotebook.Tab', padding=(16, 8))

    def _build(self):
        wrapper = ttk.Frame(self, padding=(14, 12))
        wrapper.pack(fill='both', expand=True)
        ttk.Label(wrapper, text='Anime Line Finder', style='Title.TLabel').pack(anchor='w')
        ttk.Label(wrapper, text='動画から日本語字幕を自動作成し、作品内のセリフを横断検索します。', style='Subtitle.TLabel').pack(anchor='w', pady=(1, 10))
        project_row = ttk.Frame(wrapper)
        project_row.pack(fill='x', pady=(0, 10))
        ttk.Label(project_row, text='作品：').pack(side='left')
        self.project_combo = ttk.Combobox(project_row, width=30, state='readonly')
        self.project_combo.pack(side='left', padx=(2, 8))
        self.project_combo.bind('<<ComboboxSelected>>', self.change_project)
        ttk.Button(project_row, text='作品を追加', command=self.create_project).pack(side='left', padx=(0, 5))
        ttk.Button(project_row, text='作品名を変更', command=self.rename_project).pack(side='left', padx=(0, 5))
        ttk.Button(project_row, text='作品を削除', command=self.delete_project).pack(side='left')
        self.nb = ttk.Notebook(wrapper)
        self.nb.pack(fill='both', expand=True)
        self.tab_search = ttk.Frame(self.nb, padding=8)
        self.tab_transcribe = ttk.Frame(self.nb, padding=8)
        self.tab_terms = ttk.Frame(self.nb, padding=8)
        self.tab_settings = ttk.Frame(self.nb, padding=8)
        self.nb.add(self.tab_search, text='セリフ検索')
        self.nb.add(self.tab_transcribe, text='動画・文字起こし')
        self.nb.add(self.tab_terms, text='登場人物・固有名詞')
        self.nb.add(self.tab_settings, text='設定')
        self._build_search()
        self._build_transcription()
        self._build_terms()
        self._build_settings()
        self.status_var = tk.StringVar(value='準備完了')
        ttk.Label(wrapper, textvariable=self.status_var, anchor='w').pack(fill='x', pady=(7, 0))
        if HAS_DND:
            self.drop_target_register(DND_FILES)
            self.dnd_bind('<<Drop>>', self.on_drop)

    def _build_search(self):
        root = self.tab_search
        bar = ttk.Frame(root)
        bar.pack(fill='x', pady=(0, 7))
        ttk.Label(bar, text='検索するセリフ：').pack(side='left')
        self.query_var = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.query_var, font=('Meiryo UI', 11))
        entry.pack(side='left', fill='x', expand=True, padx=(3, 8))
        entry.bind('<KeyRelease>', self.queue_search)
        entry.bind('<Return>', lambda _e: self.run_search())
        ttk.Button(bar, text='検索', command=self.run_search).pack(side='left')
        ttk.Button(bar, text='CSV出力', command=self.export_csv).pack(side='left', padx=(6, 0))
        self.hide_suspect_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text='疑わしい字幕を隠す', variable=self.hide_suspect_var,
                        command=self.run_search).pack(side='left', padx=(12, 0))
        self.search_count = tk.StringVar(value='全セリフを表示中（検索語を入力すると絞り込みます）')
        ttk.Label(root, textvariable=self.search_count).pack(anchor='w', pady=(0, 5))
        box = ttk.Frame(root)
        box.pack(fill='both', expand=True)
        cols = ('flag', 'episode', 'time', 'speech')
        self.search_tree = ttk.Treeview(box, columns=cols, show='headings', selectmode='browse')
        for c, title, width, anchor in [('flag', '確認', 45, 'center'), ('episode', '話数 / 動画', 240, 'w'), ('time', '時刻', 90, 'center'), ('speech', 'セリフ', 620, 'w')]:
            self.search_tree.heading(c, text=title)
            self.search_tree.column(c, width=width, anchor=anchor, stretch=(c == 'speech'))
        vs = ttk.Scrollbar(box, orient='vertical', command=self.search_tree.yview)
        self.search_tree.configure(yscrollcommand=vs.set)
        self.search_tree.pack(side='left', fill='both', expand=True)
        vs.pack(side='right', fill='y')
        self.search_tree.tag_configure('suspect', background='#fff0d7')
        self.search_tree.bind('<<TreeviewSelect>>', self.show_detail)
        self.search_tree.bind('<Double-1>', lambda _e: self.play_selection())
        self.search_tree.bind('<Control-c>', lambda _e: self.copy_selected())
        ttk.Label(root, text='選択行の前後のセリフ：').pack(anchor='w', pady=(8, 2))
        self.detail = tk.Text(root, height=4, wrap='word', font=('Meiryo UI', 10), state='disabled', background='#f7f8fb')
        self.detail.pack(fill='x')
        adjust_row = ttk.Frame(root)
        adjust_row.pack(fill='x', pady=(6, 0))
        ttk.Label(adjust_row, text='選択セリフの再生位置：').pack(side='left')
        self.adjustment_label = tk.StringVar(value='未選択')
        ttk.Label(adjust_row, textvariable=self.adjustment_label, width=19).pack(side='left')
        ttk.Button(adjust_row, text='0.5秒早く', command=lambda: self.adjust_playback(-0.5)).pack(side='left', padx=(4, 0))
        ttk.Button(adjust_row, text='0.5秒遅く', command=lambda: self.adjust_playback(0.5)).pack(side='left', padx=(4, 0))
        ttk.Button(adjust_row, text='補正をリセット', command=self.reset_playback).pack(side='left', padx=(4, 0))
        ttk.Label(adjust_row, text='補正値はこのセリフだけに保存', foreground='#555').pack(side='left', padx=(9, 0))
        actions = ttk.Frame(root)
        actions.pack(fill='x', pady=(7, 0))
        ttk.Button(actions, text='▶ 該当場面を再生（ダブルクリック）', command=self.play_selection).pack(side='left')
        ttk.Button(actions, text='セリフをコピー', command=self.copy_selected).pack(side='left', padx=(7, 0))
        ttk.Button(actions, text='SRTを開く', command=self.open_selected_srt).pack(side='left', padx=(7, 0))
        ttk.Label(actions, text='再生前の余裕：').pack(side='left', padx=(14, 2))
        ttk.Spinbox(actions, from_=0, to=5, increment=0.5, width=4,
                    textvariable=self.preroll_var).pack(side='left')
        ttk.Label(actions, text='秒', foreground='#666').pack(side='left')
        ttk.Label(actions, text='※ 時刻は推定値', foreground='#666').pack(side='right')

    def _build_transcription(self):
        root = self.tab_transcribe
        top = ttk.Frame(root)
        top.pack(fill='x', pady=(0, 6))
        ttk.Button(top, text='動画ファイル追加', command=self.add_files).pack(side='left', padx=(0, 4))
        ttk.Button(top, text='フォルダ追加（サブフォルダ含む）', command=self.add_folder).pack(side='left', padx=(0, 4))
        ttk.Button(top, text='選択行を削除', command=self.remove_videos).pack(side='left', padx=(0, 4))
        ttk.Button(top, text='選択動画に既存SRTを取り込む', command=self.import_srt).pack(side='left')
        cols = ('status', 'file', 'path')
        tree_box = ttk.Frame(root)
        tree_box.pack(fill='both', expand=True)
        self.video_tree = ttk.Treeview(tree_box, columns=cols, show='headings', selectmode='extended')
        for key, title, width in [('status', '状態', 120), ('file', '動画ファイル名', 350), ('path', 'フルパス', 520)]:
            self.video_tree.heading(key, text=title)
            self.video_tree.column(key, width=width, stretch=(key == 'path'))
        self.video_tree.pack(side='left', fill='both', expand=True)
        scroll = ttk.Scrollbar(tree_box, orient='vertical', command=self.video_tree.yview)
        scroll.pack(side='right', fill='y')
        self.video_tree.configure(yscrollcommand=scroll.set)
        self.video_tree.bind('<Control-a>', self.select_all_videos)
        self.video_tree.bind('<Delete>', lambda _e: self.remove_videos())
        ttk.Label(root, text='動画本体は移動・変更しません。生成SRTは作品別の保存フォルダに置きます。\n'
                             '同名のASSやMKV内のASSは読み込みません。', foreground='#555').pack(anchor='w', pady=(6, 5))
        buttons = ttk.Frame(root)
        buttons.pack(fill='x', pady=(0, 8))
        self.run_button = ttk.Button(buttons, text='未処理の動画を文字起こし', command=self.start_transcription)
        self.run_button.pack(side='left', padx=(0, 5))
        self.redo_button = ttk.Button(buttons, text='選択動画を再文字起こし', command=lambda: self.start_transcription(force=True))
        self.redo_button.pack(side='left', padx=(0, 5))
        self.cancel_button = ttk.Button(buttons, text='キャンセル', command=self.cancel_transcription, state='disabled')
        self.cancel_button.pack(side='left')
        ttk.Button(buttons, text='既存SRTの区切りを短くする', command=self.resplit_saved_srt).pack(side='left', padx=(6, 0))
        ttk.Button(buttons, text='保存SRTフォルダを開く', command=self.open_srt_folder).pack(side='right')
        segment_row = ttk.Frame(root)
        segment_row.pack(fill='x', pady=(0, 5))
        self.max_chars_var = tk.StringVar(value=str(self.store.settings.get('max_chars', 24)))
        self.max_seconds_var = tk.StringVar(value=str(self.store.settings.get('max_seconds', 4.0)))
        self.repeat_guard_var = tk.StringVar(value=str(self.store.settings.get('no_repeat_ngram_size', 5)))
        ttk.Label(segment_row, text='セリフの区切り：最大').pack(side='left')
        ttk.Spinbox(segment_row, textvariable=self.max_chars_var, from_=8, to=80, increment=1,
                    width=5).pack(side='left', padx=(4, 4))
        ttk.Label(segment_row, text='文字 ／ 最大').pack(side='left')
        ttk.Spinbox(segment_row, textvariable=self.max_seconds_var, from_=1.0, to=15.0, increment=0.5,
                    width=5).pack(side='left', padx=(4, 4))
        ttk.Label(segment_row, text='秒（既存SRTは後から調整可。旧SRTの時刻は推定）', foreground='#555').pack(side='left')
        asr_row = ttk.Frame(root)
        asr_row.pack(fill='x', pady=(0, 4))
        ttk.Label(asr_row, text='Anime Whisperの繰り返し抑制（次回の文字起こしから適用）：').pack(side='left')
        ttk.Combobox(asr_row, textvariable=self.repeat_guard_var, state='readonly', width=6,
                     values=['5', '10', '0']).pack(side='left', padx=(4, 6))
        ttk.Label(asr_row, text='5=推奨 / 10=弱め / 0=無効。誤認識を防げるとは限りません。', foreground='#555').pack(side='left')
        vad_row = ttk.Frame(root)
        vad_row.pack(fill='x', pady=(2, 6))
        self.vad_var = tk.BooleanVar(value=bool(self.store.settings.get('vad_enabled', True)))
        self.vad_threshold_var = tk.StringVar(value=str(self.store.settings.get('vad_threshold', 0.35)))
        ttk.Checkbutton(vad_row, text='無発話区間を除外（Silero VAD／推奨）', variable=self.vad_var).pack(side='left')
        ttk.Label(vad_row, text='判定しきい値：').pack(side='left', padx=(12, 0))
        ttk.Combobox(vad_row, state='readonly', textvariable=self.vad_threshold_var, width=6,
                     values=('0.25', '0.35', '0.45', '0.55', '0.65')).pack(side='left', padx=(3, 6))
        ttk.Label(vad_row, text='小さいほど小声も拾う／大きいほど発話を厳選。BGMの除去はしません。', foreground='#555').pack(side='left')
        song_row = ttk.Frame(root)
        song_row.pack(fill='x', pady=(0, 6))
        self.skip_songs_var = tk.BooleanVar(value=bool(self.store.settings.get('skip_songs', True)))
        ttk.Checkbutton(song_row, text='OP・EDなどの歌を字幕にしない（Qwen3-ASRのみ）', variable=self.skip_songs_var).pack(side='left')
        ttk.Label(song_row, text='30秒以上続く歌を判定して除外します。初回は判定用モデル約350MBをダウンロードします。',
                  foreground='#555').pack(side='left', padx=(8, 0))
        self.progress_var = tk.DoubleVar(value=0.)
        self.progress_label = tk.StringVar(value='未処理の動画を選択して開始できます。')
        ttk.Label(root, textvariable=self.progress_label).pack(fill='x')
        ttk.Progressbar(root, variable=self.progress_var, maximum=100).pack(fill='x', pady=(4, 7))
        logbox = ttk.Frame(root)
        logbox.pack(fill='both', expand=False)
        self.log_text = tk.Text(logbox, height=9, wrap='word', font=('Consolas', 9), background='#101828', foreground='#e6edf5', insertbackground='white')
        self.log_text.pack(side='left', fill='both', expand=True)
        scrollbar = ttk.Scrollbar(logbox, orient='vertical', command=self.log_text.yview)
        scrollbar.pack(side='right', fill='y')
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log('ツール起動。標準エンジンは公式Anime Whisperです。初回は設定タブから環境を導入してください。')
        if HAS_DND:
            self.video_tree.drop_target_register(DND_FILES)
            self.video_tree.dnd_bind('<<Drop>>', self.on_drop)
        else:
            self.log('ドラッグ＆ドロップを有効にするには tkinterdnd2 が必要です（起動.batで導入）。')

    def _build_terms(self):
        root = self.tab_terms
        ttk.Label(root, text='作品別の登場人物名・読み・別名を管理します。検索時の別名照合に使われます。',
                  font=('Meiryo UI', 10)).pack(anchor='w', pady=(0, 4))
        ttk.Label(root, text='※ 公式Anime Whisperには初期プロンプトを渡しません（モデル作者の推奨）。Purfview使用時のみ認識ヒントに利用します。', foreground='#666').pack(anchor='w', pady=(0, 8))
        box = ttk.Frame(root)
        box.pack(fill='both', expand=True)
        self.terms_tree = ttk.Treeview(box, show='headings', columns=('name', 'reading', 'aliases'), selectmode='extended')
        for col, title, width in [('name', '正式表記', 230), ('reading', '読み', 240), ('aliases', '別表記・呼び名', 500)]:
            self.terms_tree.heading(col, text=title)
            self.terms_tree.column(col, width=width, stretch=(col == 'aliases'))
        self.terms_tree.pack(side='left', fill='both', expand=True)
        scroll = ttk.Scrollbar(box, command=self.terms_tree.yview)
        scroll.pack(side='right', fill='y')
        self.terms_tree.configure(yscrollcommand=scroll.set)
        self.terms_tree.bind('<Double-1>', lambda _e: self.edit_term())
        actions = ttk.Frame(root)
        actions.pack(fill='x', pady=(9, 0))
        ttk.Button(actions, text='登録', command=self.add_term).pack(side='left', padx=(0, 5))
        ttk.Button(actions, text='編集（ダブルクリック）', command=self.edit_term).pack(side='left', padx=(0, 5))
        ttk.Button(actions, text='削除', command=self.remove_terms).pack(side='left')
        ttk.Separator(actions, orient='vertical').pack(side='left', fill='y', padx=12)
        self.wiki_import_button = ttk.Button(actions, text='Wikipediaから取得', command=lambda: self.start_term_import('Wikipedia'))
        self.wiki_import_button.pack(side='left', padx=(0, 5))
        self.annict_import_button = ttk.Button(actions, text='Annictから取得', command=lambda: self.start_term_import('Annict'))
        self.annict_import_button.pack(side='left', padx=(0, 5))
        self.term_info = tk.StringVar(value='')
        ttk.Label(root, textvariable=self.term_info, foreground='#555').pack(anchor='e', pady=(5, 0))

    def _build_settings(self):
        root = self.tab_settings
        ttk.Label(root, text='ローカル音声認識の設定', font=('Meiryo UI', 12, 'bold')).pack(anchor='w', pady=(0, 9))
        self.engine_var = tk.StringVar(value=ENGINE_ANIME)
        self.exe_var = tk.StringVar()
        self.model_var = tk.StringVar()
        self.model_dir_var = tk.StringVar()
        self.player_var = tk.StringVar()
        self.ffmpeg_var = tk.StringVar()
        ttk.Label(root, text='文字起こしエンジン').pack(anchor='w')
        engine_picker = ttk.Combobox(root, textvariable=self.engine_var, state='readonly',
                                     values=[ENGINE_QWEN, ENGINE_ANIME, ENGINE_PURFVIEW])
        engine_picker.pack(fill='x', pady=(3, 11))
        engine_picker.bind('<<ComboboxSelected>>', lambda _e: self.toggle_engine_ui())

        self.anime_box = ttk.LabelFrame(root, text='公式 Anime Whisper（litagin/anime-whisper）', padding=11)

        ttk.Label(self.anime_box, text='Subtitle Editは不要です。初回に一度だけ専用Python環境を導入します。\n'
                  'モデル（約3GB）は初回の文字起こし時に自動取得します。以後はローカルで再利用します。',
                  foreground='#555').pack(anchor='w')
        row = ttk.Frame(self.anime_box)
        row.pack(fill='x', pady=(8, 0))
        ttk.Button(row, text='Anime Whisper実行環境を導入（初回のみ）',
                   command=self.install_anime_whisper).pack(side='left', padx=(0, 6))
        ttk.Button(row, text='導入状態を確認', command=self.check_anime_env).pack(side='left')
        ttk.Button(row, text='VADだけ追加導入', command=self.install_vad).pack(side='left', padx=(7, 0))
        ttk.Label(self.anime_box,
                  text='GPU対応PyTorch・Transformers・FFmpegを専用環境へ導入します（大容量）。\n'
                       '公式モデルの警告に従い、固有名詞辞書を初期プロンプトとしては渡しません。',
                  foreground='#555').pack(anchor='w', pady=(9, 0))

        self.qwen_model_var = tk.StringVar(value=QWEN_MODEL_LABELS['anime'])
        self.qwen_box = ttk.LabelFrame(root, text='Qwen3-ASR ＋ Qwen3-ForcedAligner', padding=11)
        ttk.Label(self.qwen_box, text='認識モデル').pack(anchor='w')
        ttk.Combobox(self.qwen_box, textvariable=self.qwen_model_var, state='readonly',
                     values=list(QWEN_MODEL_LABELS.values())).pack(fill='x', pady=(3, 6))
        ttk.Label(self.qwen_box, text='字幕の時刻はForcedAlignerが音声から推定します（文字数による推定ではありません）。\n'
                  'Anime Whisperとは別の専用Python環境を使うため、既存環境には影響しません。\n'
                  'モデル（合計約5GB）は初回の文字起こし時に自動取得します。追加学習版のライセンスはモデルページを確認してください。',
                  foreground='#555').pack(anchor='w')
        row = ttk.Frame(self.qwen_box)
        row.pack(fill='x', pady=(8, 0))
        ttk.Button(row, text='Qwen3-ASR実行環境を導入（初回のみ）',
                   command=self.install_qwen).pack(side='left', padx=(0, 6))
        ttk.Button(row, text='導入状態を確認', command=self.check_qwen_env).pack(side='left')

        self.purfview_box = ttk.LabelFrame(root, text='Purfviewエンジン（従来方式）', padding=11)
        self.model_combo = None
        for label, var, typ in [
                ('Whisperエンジン（faster-whisper-xxl.exe）', self.exe_var, 'file'),
                ('モデル格納フォルダ（_models）', self.model_dir_var, 'dir'),
                ('モデル名（例：anime.ja）', self.model_var, 'model')]:
            one = ttk.Frame(self.purfview_box)
            one.pack(fill='x', pady=(0, 5))
            ttk.Label(one, text=label).pack(anchor='w')
            fields = ttk.Frame(one)
            fields.pack(fill='x')
            if typ == 'model':
                self.model_combo = ttk.Combobox(fields, textvariable=var)
                self.model_combo.pack(side='left', fill='x', expand=True)
                ttk.Button(fields, text='一覧更新', command=self.update_model_list).pack(side='left', padx=(5, 0))
            else:
                ttk.Entry(fields, textvariable=var).pack(side='left', fill='x', expand=True)
                ttk.Button(fields, text='参照', command=lambda v=var, t=typ: self.browse_setting(v, t)).pack(side='left', padx=(5, 0))
        ttk.Button(self.purfview_box, text='Subtitle Editのエンジンを自動検索',
                   command=self.auto_find_engine).pack(anchor='w')

        self.common_anchor = ttk.Frame(root)
        self.common_anchor.pack(fill='x')
        for label, var in [
            ('動画プレーヤー（MPC-HC / MPC-BE / mpv / VLC / ffplay。空欄なら自動検出）', self.player_var),
            ('FFmpeg（省略可能。未設定ならAnime Whisper用の内蔵FFmpegを利用）', self.ffmpeg_var),
        ]:
            row = ttk.Frame(root)
            row.pack(fill='x', pady=(9, 0))
            ttk.Label(row, text=label).pack(anchor='w')
            inside = ttk.Frame(row)
            inside.pack(fill='x')
            ttk.Entry(inside, textvariable=var).pack(side='left', fill='x', expand=True)
            ttk.Button(inside, text='参照', command=lambda v=var: self.browse_setting(v, 'file')).pack(side='left', padx=(5, 0))
        ttk.Button(root, text='設定を保存', command=self.save_settings).pack(anchor='w', pady=(12, 0))
        ttk.Label(root, text='既存の動画・SRT・登録した固有名詞はそのまま引き継ぎます。\n'
                  '字幕保存先：%LOCALAPPDATA%\\AnimeLineFinder\\works\\作品ID\\srt （Windows）',
                  foreground='#555').pack(anchor='w', pady=(8, 0))
        self.toggle_engine_ui()

    def toggle_engine_ui(self):
        boxes = {ENGINE_ANIME: self.anime_box, ENGINE_PURFVIEW: self.purfview_box, ENGINE_QWEN: self.qwen_box}
        chosen = boxes.get(self.engine_var.get(), self.anime_box)
        for box in boxes.values():
            if box is not chosen:
                box.pack_forget()
        chosen.pack(fill='x', pady=(3, 5), before=self.common_anchor)

    def qwen_model_key(self):
        label = self.qwen_model_var.get()
        return next((k for k, v in QWEN_MODEL_LABELS.items() if v == label), 'anime')

    def install_qwen(self):
        installer = app_folder() / 'Qwen3ASR_導入.bat'
        if sys.platform != 'win32':
            messagebox.showinfo('Windows専用', 'この自動導入ボタンはWindows専用です。')
            return
        if not installer.is_file():
            messagebox.showerror('導入ファイルなし', f'導入バッチが見つかりません：\n{installer}')
            return
        try:
            os.startfile(str(installer))
        except OSError as exc:
            messagebox.showerror('起動エラー', str(exc))

    def check_qwen_env(self):
        python = qwen_python()
        if not python.is_file():
            messagebox.showinfo('Qwen3-ASR環境', 'まだ導入されていません。\n「Qwen3-ASR実行環境を導入」を押してください。')
            return
        try:
            result = subprocess.run([str(python), '-c',
                'import torch,transformers,silero_vad,nagisa; import qwen_asr; print("PyTorch:",torch.__version__); print("CUDA:",torch.cuda.is_available()); print("Transformers:",transformers.__version__); print("qwen-asr: 導入済み")'],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                creationflags=popen_flags(), timeout=120)
        except Exception as exc:
            messagebox.showerror('環境確認', str(exc))
            return
        if result.returncode:
            messagebox.showerror('導入不完全', (result.stderr or result.stdout)[-1200:])
        else:
            messagebox.showinfo('Qwen3-ASR環境', result.stdout.strip() +
                '\n\nモデルは初回の文字起こし時に自動ダウンロードされます。')

    def install_anime_whisper(self):
        installer = app_folder() / 'AnimeWhisper_導入.bat'
        if sys.platform != 'win32':
            messagebox.showinfo('Windows専用', 'この自動導入ボタンはWindows専用です。')
            return
        if not installer.is_file():
            messagebox.showerror('導入ファイルなし', f'導入バッチが見つかりません：\n{installer}')
            return
        try:
            os.startfile(str(installer))
        except OSError as exc:
            messagebox.showerror('起動エラー', str(exc))

    def install_vad(self):
        installer = app_folder() / 'VAD_追加導入.bat'
        if sys.platform != 'win32':
            messagebox.showinfo('Windows専用', 'この自動導入ボタンはWindows専用です。')
            return
        if not installer.is_file():
            messagebox.showerror('導入ファイルなし', f'VADの追加導入バッチが見つかりません：\n{installer}')
            return
        try:
            os.startfile(str(installer))
        except OSError as exc:
            messagebox.showerror('起動エラー', str(exc))

    def check_anime_env(self):
        python = anime_python()
        if not python.is_file():
            messagebox.showinfo('Anime Whisper環境', 'まだ導入されていません。\n「Anime Whisper実行環境を導入」を押してください。')
            return
        try:
            result = subprocess.run([str(python), '-c',
                'import importlib.util,torch,transformers,imageio_ffmpeg; print("PyTorch:",torch.__version__); print("CUDA:",torch.cuda.is_available()); print("Transformers:",transformers.__version__); print("Silero VAD:", "導入済み" if importlib.util.find_spec("silero_vad") else "未導入")'],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                creationflags=popen_flags(), timeout=60)
        except Exception as exc:
            messagebox.showerror('環境確認', str(exc))
            return
        if result.returncode:
            messagebox.showerror('導入不完全', (result.stderr or result.stdout)[-1200:])
        else:
            messagebox.showinfo('Anime Whisper環境', result.stdout.strip() +
                '\n\nモデルは初回の文字起こし時に自動ダウンロードされます。')

    def log(self, text):
        self.log_text.insert('end', str(text).rstrip() + '\n')
        if int(self.log_text.index('end-1c').split('.')[0]) > 2500:
            self.log_text.delete('1.0', '600.0')
        self.log_text.see('end')

    def refresh_project_list(self):
        projects = self.store.catalog['projects']
        self.project_combo['values'] = [x['name'] for x in projects]
        for i, x in enumerate(projects):
            if x['id'] == self.store.project_id:
                self.project_combo.current(i)
                break

    def change_project(self, _event=None):
        if self.is_working():
            messagebox.showinfo('処理中', '文字起こし中は作品を切り替えられません。')
            self.refresh_project_list()
            return
        index = self.project_combo.current()
        if index < 0:
            return
        self.store.open_project(self.store.catalog['projects'][index]['id'])
        self.refresh_videos()
        self.refresh_terms()
        self.run_search()
        self.status_var.set(f'作品「{self.store.project["name"]}」を開きました')

    def create_project(self):
        if self.is_working():
            return
        name = simpledialog.askstring('作品を追加', '作品名を入力してください：', parent=self)
        if name and name.strip():
            self.store.create_project(name.strip())
            self.refresh_project_list()
            self.refresh_videos()
            self.refresh_terms()
            self.run_search()
            self.nb.select(self.tab_terms)
            self.status_var.set('新しい作品を追加しました。Wikipedia／Annictから登場人物を取得できます。')

    def rename_project(self):
        name = simpledialog.askstring('作品名を変更', '新しい作品名：', initialvalue=self.store.project['name'], parent=self)
        if name and name.strip():
            self.store.rename_project(name.strip())
            self.refresh_project_list()

    def delete_project(self):
        if self.is_working():
            return
        if len(self.store.catalog['projects']) <= 1:
            messagebox.showinfo('作品削除', '最後の1作品は削除できません。')
            return
        name = self.store.project['name']
        if not messagebox.askyesno('削除の確認', f'「{name}」を一覧から削除しますか？\n'
                                    '作品の管理データと生成SRTも削除されます。元の動画は削除されません。'):
            return
        ident = self.store.project_id
        self.store.catalog['projects'] = [p for p in self.store.catalog['projects'] if p['id'] != ident]
        # Only delete own application data, never original videos.
        shutil.rmtree(self.store.project_dir(), ignore_errors=True)
        self.store.open_project(self.store.catalog['projects'][0]['id'])
        self.refresh_project_list()
        self.refresh_videos()
        self.refresh_terms()
        self.run_search()

    def refresh_videos(self):
        tree = self.video_tree
        tree.delete(*tree.get_children())
        for i, video in enumerate(self.store.project['videos']):
            source = Path(video['path'])
            if not source.exists():
                state = '動画なし'
            elif self.store.srt_path(video['path']).exists():
                state = video.get('status', '完了') if video.get('status') in ('完了', 'SRT取込済') else '字幕あり'
            else:
                state = video.get('status', '未処理')
            tree.insert('', 'end', iid=str(i), values=(state, source.name, str(source)))
        self.status_var.set(f'{self.store.project["name"]}：動画{len(self.store.project["videos"])}件')

    def on_drop(self, event):
        try:
            files = self.tk.splitlist(event.data)
        except tk.TclError:
            files = [event.data]
        self.add_media_paths(files)
        self.nb.select(self.tab_transcribe)

    def add_media_paths(self, files):
        if self.is_working():
            messagebox.showinfo('処理中', '文字起こし中は動画を追加できません。')
            return
        count = self.store.add_paths(files)
        self.refresh_videos()
        self.log(f'動画 {count} 件を追加しました（既存と重複するファイルは除外）。')

    def add_files(self):
        files = filedialog.askopenfilenames(parent=self, title='動画ファイルを選択',
                                            filetypes=[('動画ファイル', '*.mkv *.mp4 *.avi *.mov *.webm *.m4v *.ts *.m2ts'), ('すべて', '*.*')])
        if files:
            self.add_media_paths(files)

    def add_folder(self):
        folder = filedialog.askdirectory(parent=self, title='作品の動画があるフォルダを選択')
        if folder:
            self.add_media_paths([folder])

    def selected_videos(self):
        selected = []
        for iid in self.video_tree.selection():
            try:
                selected.append(self.store.project['videos'][int(iid)])
            except (IndexError, ValueError):
                pass
        return selected

    def select_all_videos(self, _e=None):
        self.video_tree.selection_set(self.video_tree.get_children())
        return 'break'

    def remove_videos(self):
        if self.is_working():
            return
        selected = [int(i) for i in self.video_tree.selection()]
        if not selected:
            return
        if not messagebox.askyesno('確認', f'選択した {len(selected)} 件を作品の一覧から外しますか？\n'
                                    '既に作ったSRTは保存フォルダに残ります。'):
            return
        for index in sorted(selected, reverse=True):
            del self.store.project['videos'][index]
        self.store.save_project()
        self.refresh_videos()
        self.run_search()

    def import_srt(self):
        if self.is_working():
            return
        video = self.selected_videos()
        if len(video) != 1:
            messagebox.showinfo('SRT取込', '動画を1件選択してください。')
            return
        src = filedialog.askopenfilename(title='取り込むSRTを選択', filetypes=[('SRT字幕', '*.srt')])
        if not src:
            return
        try:
            self.store.import_srt(video[0], Path(src))
        except Exception as e:
            messagebox.showerror('取込エラー', str(e))
            return
        self.refresh_videos()
        self.log(f'SRT取込：{Path(src).name}')
        self.run_search()

    def open_srt_folder(self):
        folder = self.store.project_dir() / 'srt'
        folder.mkdir(parents=True, exist_ok=True)
        open_in_default(folder)

    def refresh_terms(self):
        self.terms_tree.delete(*self.terms_tree.get_children())
        terms = self.store.project['terms']
        for i, item in enumerate(terms):
            self.terms_tree.insert('', 'end', iid=str(i), values=(item.get('name', ''), item.get('reading', ''), item.get('aliases', '')))
        self.term_info.set(f'登録 {len(terms)} 件 ｜ Anime Whisper: 検索辞書用 / Purfview: 認識ヒントにも利用')

    def add_term(self):
        dialog = TermDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            self.store.project['terms'].append(dialog.result)
            self.store.save_project()
            self.refresh_terms()

    def edit_term(self):
        selection = self.terms_tree.selection()
        if len(selection) != 1:
            return
        index = int(selection[0])
        dialog = TermDialog(self, self.store.project['terms'][index])
        self.wait_window(dialog)
        if dialog.result:
            self.store.project['terms'][index] = dialog.result
            self.store.save_project()
            self.refresh_terms()
            self.run_search()

    def remove_terms(self):
        indices = sorted([int(s) for s in self.terms_tree.selection()], reverse=True)
        if not indices:
            return
        for i in indices:
            del self.store.project['terms'][i]
        self.store.save_project()
        self.refresh_terms()
        self.run_search()

    def start_term_import(self, provider):
        if self.import_busy:
            messagebox.showinfo('取得中', '現在の取得処理が終了してから再度お試しください。')
            return
        query = simpledialog.askstring(f'{provider}で作品を検索', '検索する作品名（Wikipediaは記事URLでも可）：' if provider == 'Wikipedia' else '検索する作品名：',
                                       initialvalue=self.store.project['name'], parent=self)
        if not query or not query.strip():
            return
        token = ''
        if provider == 'Annict':
            token = simpledialog.askstring('Annict APIの認証',
                'Annictの「読み取り専用」の個人用アクセストークンを入力してください。\n'
                '（このツールには保存されません。他人に教えないでください。）\n'
                '発行方法：https://annict.com/oauth/applications',
                show='*', parent=self)
            if not token:
                return
        self.import_busy = True
        self.wiki_import_button.configure(state='disabled')
        self.annict_import_button.configure(state='disabled')
        self.status_var.set(f'{provider}で「{query.strip()}」を検索中…')
        project_id = self.store.project_id
        threading.Thread(target=self._term_import_search_worker,
                         args=(provider, query.strip(), token, project_id), daemon=True).start()

    def _term_import_search_worker(self, provider, query, token, project_id):
        try:
            items = wikipedia_search(query) if provider == 'Wikipedia' else annict_search(query, token)
            self.events.put(('import_works', provider, items, token, project_id))
        except Exception as exc:
            self.events.put(('import_error', provider, str(exc), project_id))

    def _term_import_fetch_worker(self, provider, item, token, project_id):
        try:
            terms = wikipedia_terms(item['id']) if provider == 'Wikipedia' else annict_terms(item['id'], token)
            self.events.put(('import_terms', provider, item, terms, project_id))
        except Exception as exc:
            self.events.put(('import_error', provider, str(exc), project_id))

    def _finish_import(self):
        self.import_busy = False
        self.wiki_import_button.configure(state='normal')
        self.annict_import_button.configure(state='normal')

    def _handle_import_works(self, provider, items, token, project_id):
        if project_id != self.store.project_id:
            self._finish_import()
            self.status_var.set('作品を切り替えたため、取得結果を破棄しました。')
            return
        if not items:
            self._finish_import()
            messagebox.showinfo(f'{provider}：作品なし', '検索条件に一致する作品が見つかりませんでした。\n作品名を変えて試してください。')
            return
        chooser = ChooseArticleDialog(self, items, provider)
        self.wait_window(chooser)
        if not chooser.result:
            self._finish_import()
            return
        self.status_var.set(f'{provider}：「{chooser.result["title"]}」の登場人物を取得中…')
        threading.Thread(target=self._term_import_fetch_worker,
                         args=(provider, chooser.result, token, project_id), daemon=True).start()

    def _handle_import_terms(self, provider, item, terms, project_id):
        self._finish_import()
        if project_id != self.store.project_id:
            self.status_var.set('作品を切り替えたため、取得結果を破棄しました。')
            return
        if not terms:
            messagebox.showinfo(f'{provider}：候補なし', '記事の登場人物欄（またはキャストデータ）から候補を抽出できませんでした。\n'
                                '作品によっては取得できない場合があります。')
            return
        preview = ImportPreviewDialog(self, terms, provider, item['title'], item['url'])
        self.wait_window(preview)
        if preview.result is None:
            self.status_var.set('固有名詞の取り込みをキャンセルしました。')
            return
        merged, added, updated = merge_terms(self.store.project['terms'], preview.result)
        self.store.project['terms'] = merged
        self.store.save_project()
        self.refresh_terms()
        self.run_search()
        self.status_var.set(f'{provider}からの取込：{added}件追加、{updated}件の未入力項目を補完（既存データは保持）')
        messagebox.showinfo('固有名詞の取り込み完了', f'{added}件追加、{updated}件の未入力項目を補完しました。\n'
                            '既に登録済みの正式表記・読みは上書きしません。')

    def load_settings(self):
        opts = self.store.settings
        if not opts.get('exe'):
            opts['exe'] = find_engine()
        if not opts.get('model_dir') and opts.get('exe'):
            model_root = find_model_root(opts['exe'])
            if model_root.is_dir():
                opts['model_dir'] = str(model_root)
        if not opts.get('ffmpeg'):
            opts['ffmpeg'] = find_ffmpeg(opts.get('exe', ''))
        self.exe_var.set(opts.get('exe', ''))
        self.model_dir_var.set(opts.get('model_dir', ''))
        self.model_var.set(opts.get('model', 'anime.ja'))
        self.engine_var.set(ENGINE_LABELS.get(opts.get('engine'), ENGINE_ANIME))
        self.qwen_model_var.set(QWEN_MODEL_LABELS.get(opts.get('qwen_model'), QWEN_MODEL_LABELS['anime']))
        self.toggle_engine_ui()
        self.player_var.set(opts.get('player', ''))
        self.ffmpeg_var.set(opts.get('ffmpeg', ''))
        self.max_chars_var.set(str(opts.get('max_chars', 24)))
        self.max_seconds_var.set(str(opts.get('max_seconds', 4.0)))
        self.repeat_guard_var.set(str(opts.get('no_repeat_ngram_size', 5)))
        self.vad_var.set(bool(opts.get('vad_enabled', True)))
        self.vad_threshold_var.set(str(opts.get('vad_threshold', 0.35)))
        self.skip_songs_var.set(bool(opts.get('skip_songs', True)))
        self.preroll_var.set(str(opts.get('play_preroll', 0.5)))
        self.update_model_list()

    def browse_setting(self, var, typ):
        if typ == 'dir':
            answer = filedialog.askdirectory(title='モデルの格納フォルダを指定')
        else:
            answer = filedialog.askopenfilename(title='実行ファイルを指定', filetypes=[('Windows実行ファイル', '*.exe'), ('すべてのファイル', '*.*')])
        if answer:
            var.set(answer)
            if var is self.exe_var:
                root = find_model_root(answer)
                if root.is_dir():
                    self.model_dir_var.set(str(root))
            self.update_model_list()

    def auto_find_engine(self):
        found = find_engine()
        if not found:
            messagebox.showinfo('自動検索', '自動検出できませんでした。\nSubtitle Editで Ctrl+Alt+Shift+D を押してデータフォルダを開き、\nSpeechToText\\Purfview-Faster-Whisper-XXL\\faster-whisper-xxl.exe を参照してください。')
            return
        self.exe_var.set(found)
        root = find_model_root(found)
        if root.is_dir():
            self.model_dir_var.set(str(root))
        if not self.ffmpeg_var.get():
            self.ffmpeg_var.set(find_ffmpeg(found))
        self.update_model_list()
        self.status_var.set('Subtitle EditのWhisperエンジンが見つかりました。')

    def update_model_list(self):
        root = Path(self.model_dir_var.get().strip()) if self.model_dir_var.get().strip() else None
        values = []
        if root and root.is_dir():
            values = sorted((x.name for x in root.iterdir() if x.is_dir()), key=str.casefold)
        self.model_combo['values'] = values
        if not self.model_var.get():
            self.model_var.set('anime.ja')

    def segment_limits(self):
        try:
            chars = int(self.max_chars_var.get())
            seconds = float(self.max_seconds_var.get())
        except ValueError as exc:
            raise ValueError('区切りの文字数・秒数は数字で入力してください。') from exc
        if not (8 <= chars <= 80 and 1.0 <= seconds <= 15.0):
            raise ValueError('区切りの文字数は8～80、秒数は1～15にしてください。')
        return chars, seconds

    def resplit_saved_srt(self):
        if self.is_working():
            messagebox.showinfo('処理中', '文字起こし中は既存SRTを変更できません。')
            return
        try:
            chars, seconds = self.segment_limits()
        except ValueError as exc:
            messagebox.showerror('区切り設定', str(exc))
            return
        videos = self.selected_videos()
        if not videos:
            videos = self.store.project['videos']
            if not messagebox.askyesno('全SRTを調整',
                 '動画を選択していないため、この作品の全SRTに区切り調整を適用します。\n続行しますか？'):
                return
        changed = 0
        total = 0
        for video in videos:
            file = self.store.srt_path(video['path'])
            if not file.is_file():
                continue
            try:
                old = read_srt(file)
                new = split_cues(old, chars, seconds)
                if [(c.start, c.end, c.text) for c in old] == [(c.start, c.end, c.text) for c in new]:
                    total += 1
                    continue
                backup = file.with_suffix('.srt.before_split.bak')
                if not backup.exists():
                    shutil.copy2(file, backup)
                pending = file.with_suffix('.srt.pending')
                try:
                    write_srt(pending, new)
                    os.replace(pending, file)
                finally:
                    pending.unlink(missing_ok=True)
                changed += 1
                total += 1
                self.log(f'区切り調整：{Path(video["path"]).name}  {len(old)} → {len(new)}件')
            except (OSError, ValueError) as exc:
                self.log(f'区切り調整失敗：{file.name}: {exc}')
        self.store.settings.update({'max_chars': chars, 'max_seconds': seconds})
        self.store.save_settings()
        self.run_search()
        messagebox.showinfo('完了', f'{total}件のSRTを確認し、{changed}件を更新しました。\n'
                            '更新前のSRTは *.srt.before_split.bak に保存しています。\n'
                            '※旧字幕の分割時刻は文字量からの推定です。')

    def save_settings(self):
        try:
            max_chars, max_seconds = self.segment_limits()
        except ValueError as exc:
            messagebox.showerror('区切り設定', str(exc))
            return False
        try:
            vad_threshold = float(self.vad_threshold_var.get())
            if not (0.15 <= vad_threshold <= 0.85):
                raise ValueError
        except (ValueError, TypeError):
            messagebox.showerror('VAD設定', 'VADのしきい値は0.15～0.85の数字にしてください。')
            return False
        self.store.settings.update({'engine': ENGINE_KEYS.get(self.engine_var.get(), 'anime_whisper'),
                                    'qwen_model': self.qwen_model_key(),
                                    'exe': self.exe_var.get().strip(), 'model_dir': self.model_dir_var.get().strip(),
                                    'model': self.model_var.get().strip() or 'anime.ja',
                                    'player': self.player_var.get().strip(), 'ffmpeg': self.ffmpeg_var.get().strip(),
                                    'max_chars': max_chars, 'max_seconds': max_seconds,
                                    'play_preroll': self._preroll(),
                                    'no_repeat_ngram_size': int(self.repeat_guard_var.get()),
                                    'vad_enabled': self.vad_var.get(), 'vad_threshold': vad_threshold,
                                    'skip_songs': self.skip_songs_var.get()})
        self.store.save_settings()
        self.status_var.set('設定を保存しました')
        return True

    def is_working(self):
        return bool(self.worker and self.worker.is_alive())

    def start_transcription(self, force=False):
        if self.is_working():
            return
        if not self.save_settings():
            return
        engine = self.store.settings.get('engine', 'anime_whisper')
        if engine == 'anime_whisper':
            if not anime_python().is_file():
                self.nb.select(self.tab_settings)
                messagebox.showerror('Anime Whisper未導入',
                    '先に設定タブの「Anime Whisper実行環境を導入」を押してください。\n'
                    'モデルは初回の文字起こし開始時に自動ダウンロードします。')
                return
            if not (app_folder() / 'anime_whisper_worker.py').is_file():
                messagebox.showerror('ファイルがありません', 'anime_whisper_worker.py が見つかりません。ZIPを再展開してください。')
                return
            if self.store.settings.get('vad_enabled', True):
                # Fail early with a friendly message instead of running a 3GB model
                # and discovering the optional VAD package is missing afterwards.
                try:
                    installed = subprocess.run([str(anime_python()), '-c', 'import silero_vad'],
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                               timeout=20, creationflags=popen_flags())
                except (OSError, subprocess.TimeoutExpired):
                    installed = None
                if installed is None or installed.returncode:
                    self.nb.select(self.tab_settings)
                    messagebox.showerror('VAD未導入',
                        '無発話区間除外を使うには、設定タブの「VADだけ追加導入」を一度実行してください。\n'
                        '既存のAnime WhisperモデルやGPU環境の再導入は不要です。\n'
                        'VADを使わない場合は動画・文字起こしのチェックをOFFにしてください。')
                    return
            model = 'litagin/anime-whisper'
        elif engine == 'qwen3_asr':
            if not qwen_python().is_file():
                self.nb.select(self.tab_settings)
                messagebox.showerror('Qwen3-ASR未導入',
                    '先に設定タブの「Qwen3-ASR実行環境を導入」を押してください。\n'
                    'モデルは初回の文字起こし開始時に自動ダウンロードします。')
                return
            if not (app_folder() / 'qwen_asr_worker.py').is_file():
                messagebox.showerror('ファイルがありません', 'qwen_asr_worker.py が見つかりません。')
                return
            model = QWEN_MODEL_LABELS.get(self.store.settings.get('qwen_model', 'anime'), 'Qwen3-ASR')
        else:
            exe = Path(self.exe_var.get().strip())
            if not exe.is_file():
                self.nb.select(self.tab_settings)
                messagebox.showerror('エンジン未設定', 'Purfviewの faster-whisper-xxl.exe を指定してください。')
                return
            model = self.model_var.get().strip()
            if not model:
                messagebox.showerror('モデル未設定', 'モデル名を入力してください。')
                return
            model_dir = self.model_dir_var.get().strip()
            if model_dir and not Path(model_dir).is_dir():
                messagebox.showerror('モデルフォルダ', 'モデルの格納フォルダが存在しません。')
                return
            if model_dir and not (Path(model_dir) / model).is_dir():
                if not messagebox.askyesno('モデルの確認', f'モデルフォルダ内に「{model}」が見つかりません。\n'
                                           '名前が異なる場合はモデル一覧から選択してください。\nこのまま実行しますか？'):
                    self.nb.select(self.tab_settings)
                    return
        videos = self.selected_videos() if force else self.store.project['videos']
        if force and not videos:
            messagebox.showinfo('対象未選択', '再文字起こしする動画を選択してください。')
            return
        work = [v['path'] for v in videos if Path(v['path']).is_file() and (force or not self.store.srt_path(v['path']).exists())]
        if not work:
            messagebox.showinfo('対象なし', '処理対象がありません。すべてSRT作成済みの場合、選択して「再文字起こし」を使用してください。')
            return
        self.cancel_event.clear()
        self.progress_var.set(0)
        self.run_button['state'] = 'disabled'
        self.redo_button['state'] = 'disabled'
        self.cancel_button['state'] = 'normal'
        self.nb.select(self.tab_transcribe)
        title = self.store.project['name']
        terms = [dict(x) for x in self.store.project['terms']]
        # Freeze project output location and options while worker runs.
        dest_dir = self.store.project_dir()
        settings = dict(self.store.settings)
        self.worker = threading.Thread(target=self.transcribe_worker,
                                       args=(work, dest_dir, settings, make_prompt(title, terms)), daemon=True)
        self.worker.start()
        self.log(f'文字起こし開始：{len(work)}件。モデル={model}、BGM除去なし。')
        if engine == 'anime_whisper':
            self.log('公式モデルを直接読み込みます。初回は約3GBダウンロード。固有名詞プロンプトは使用しません。')
            self.log('Silero VAD: ' + ('ON（発話区間だけ認識）' if settings.get('vad_enabled', True) else 'OFF（従来方式）'))
        elif engine == 'qwen3_asr':
            self.log('Qwen3-ASRで認識し、ForcedAlignerで字幕時刻を付けます。初回は約5GBダウンロード。')
            self.log('Silero VAD: ' + ('ON（発話区間だけ認識）' if settings.get('vad_enabled', True) else 'OFF（固定長区間）'))

    def transcribe_worker(self, paths, project_dir, settings, prompt):
        total = len(paths)
        failures = 0
        try:
            for idx, path in enumerate(paths, 1):
                if self.cancel_event.is_set():
                    break
                self.events.put(('log', f'[{idx}/{total}] {Path(path).name}'))
                self.events.put(('progress', (idx-1) * 100 / total, f'{idx}/{total}：{Path(path).name}'))
                try:
                    with tempfile.TemporaryDirectory(prefix='stt_', dir=str(project_dir)) as temp:
                        tempdir = Path(temp)
                        if settings.get('engine', 'anime_whisper') == 'anime_whisper':
                            output_file = tempdir / 'transcribed.srt'
                            cmd = [str(anime_python()), '-u', str(app_folder() / 'anime_whisper_worker.py'),
                                   '--input', str(path), '--output', str(output_file),
                                   '--cache-dir', str(self.store.root / 'models' / 'huggingface')]
                            cmd.extend(['--max-chars', str(settings.get('max_chars', 24)),
                                        '--max-seconds', str(settings.get('max_seconds', 4.0)),
                                        '--no-repeat-ngram-size', str(settings.get('no_repeat_ngram_size', 5)),
                                        '--vad', 'on' if settings.get('vad_enabled', True) else 'off',
                                        '--vad-threshold', str(settings.get('vad_threshold', 0.35))])
                            if settings.get('ffmpeg'):
                                cmd.extend(['--ffmpeg', settings['ffmpeg']])
                            working_directory = str(app_folder())
                        elif settings.get('engine') == 'qwen3_asr':
                            output_file = tempdir / 'transcribed.srt'
                            cmd = [str(qwen_python()), '-u', str(app_folder() / 'qwen_asr_worker.py'),
                                   '--input', str(path), '--output', str(output_file),
                                   '--cache-dir', str(self.store.root / 'models' / 'huggingface'),
                                   '--model', settings.get('qwen_model', 'anime'),
                                   '--max-chars', str(settings.get('max_chars', 24)),
                                   '--max-seconds', str(settings.get('max_seconds', 4.0)),
                                   '--vad', 'on' if settings.get('vad_enabled', True) else 'off',
                                   '--vad-threshold', str(settings.get('vad_threshold', 0.35)),
                                   '--songs', 'skip' if settings.get('skip_songs', True) else 'keep']
                            if settings.get('ffmpeg'):
                                cmd.extend(['--ffmpeg', settings['ffmpeg']])
                            working_directory = str(app_folder())
                        else:
                            cmd = make_command(settings['exe'], path, settings['model'], settings.get('model_dir', ''), tempdir, prompt)
                            working_directory = str(Path(settings['exe']).parent)
                        startupinfo = None
                        if sys.platform == 'win32':
                            startupinfo = subprocess.STARTUPINFO()
                            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                        with self.proc_lock:
                            self.proc = subprocess.Popen(cmd, cwd=working_directory,
                                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                         text=True, encoding='utf-8', errors='replace',
                                                         bufsize=1, creationflags=popen_flags(),
                                                         startupinfo=startupinfo)
                        last = []
                        for line in self.proc.stdout:
                            if self.cancel_event.is_set():
                                break
                            line = line.strip()
                            if not line:
                                continue
                            last.append(line)
                            last = last[-25:]
                            self.events.put(('log', line[:800]))
                            match = re.search(r'^PROGRESS:(\d{1,3})$', line) or re.search(r'(?<!\d)(\d{1,3})(?:\.\d+)?\s*%', line)
                            if match:
                                pct = min(100, int(match.group(1)))
                                self.events.put(('progress', ((idx-1)+pct/100)*100/total,
                                                 f'{idx}/{total}：{pct}%  {Path(path).name}'))
                        if self.cancel_event.is_set():
                            with self.proc_lock:
                                if self.proc and self.proc.poll() is None:
                                    self.proc.terminate()
                            break
                        rc = self.proc.wait()
                        with self.proc_lock:
                            self.proc = None
                        generated = locate_generated_srt(tempdir, path)
                        if rc != 0:
                            raise RuntimeError(f'終了コード {rc}：' + ' / '.join(last[-4:]))
                        if generated is None:
                            raise RuntimeError('SRTが出力されませんでした。モデル名・ログを確認してください。')
                        cues = read_srt(generated)
                        if not cues:
                            raise RuntimeError('生成されたSRTにセリフが見つかりません。')
                        ident = __import__('hashlib').sha1(os.path.normcase(path).encode('utf-8')).hexdigest()[:12]
                        safe_stem = re.sub(r'[<>:"/\\|?*]', '_', Path(path).stem)[:60]
                        target = project_dir / 'srt' / f'{safe_stem}_{ident}.srt'
                        target.parent.mkdir(parents=True, exist_ok=True)
                        copied = target.with_suffix('.srt.pending')
                        cues = split_cues(cues, settings.get('max_chars', 24),
                                          settings.get('max_seconds', 4.0))
                        write_srt(copied, cues)
                        if target.exists():
                            previous = target.with_suffix('.srt.before_vad.bak')
                            if not previous.exists():
                                shutil.copy2(target, previous)
                        os.replace(copied, target)
                        self.events.put(('row_done', path, '完了'))
                        self.events.put(('log', f'✓ SRT作成：{len(cues)}行 → {target.name}'))
                except Exception as exc:
                    if not self.cancel_event.is_set():
                        failures += 1
                        self.events.put(('row_done', path, 'エラー'))
                        self.events.put(('log', f'✗ エラー: {exc}'))
                finally:
                    with self.proc_lock:
                        p = self.proc
                        self.proc = None
                    if p and p.poll() is None:
                        try:
                            p.terminate()
                        except OSError:
                            pass
                self.events.put(('progress', idx * 100 / total, f'{idx}/{total} 件目まで終了'))
            stopped = self.cancel_event.is_set()
            self.events.put(('finished', stopped, failures, total))
        except Exception:
            self.events.put(('log', traceback.format_exc()))
            self.events.put(('finished', True, failures, total))

    def cancel_transcription(self):
        if not self.is_working():
            return
        self.cancel_event.set()
        with self.proc_lock:
            proc = self.proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        self.log('キャンセル要求を送信しました。完了済みのSRTは保持します。')

    def poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == 'log':
                    self.log(event[1])
                elif kind == 'progress':
                    self.progress_var.set(event[1])
                    self.progress_label.set(event[2])
                elif kind == 'row_done':
                    video_path, status = event[1], event[2]
                    for video in self.store.project['videos']:
                        if video['path'] == video_path:
                            video['status'] = status
                            break
                    self.store.save_project()
                    self.refresh_videos()
                elif kind == 'finished':
                    stopped, failures, total = event[1:]
                    self.run_button['state'] = 'normal'
                    self.redo_button['state'] = 'normal'
                    self.cancel_button['state'] = 'disabled'
                    self.progress_label.set(('キャンセルしました' if stopped else '全件の処理が終了しました') + f'（対象{total}件・エラー{failures}件）')
                    self.log(self.progress_label.get())
                    self.run_search()
                elif kind == 'import_works':
                    self._handle_import_works(event[1], event[2], event[3], event[4])
                elif kind == 'import_terms':
                    self._handle_import_terms(event[1], event[2], event[3], event[4])
                elif kind == 'import_error':
                    self._finish_import()
                    messagebox.showerror(f'{event[1]}：取得エラー', event[2])
                    self.status_var.set(f'{event[1]}からの取得に失敗しました。')
                elif kind == 'preview_ready':
                    self.status_var.set('指定場面の短いプレビューを開きました。')
                    open_in_default(Path(event[1]))
                elif kind == 'preview_error':
                    messagebox.showerror('プレビュー失敗', event[1])
        except queue.Empty:
            pass
        self.after(130, self.poll_events)

    def queue_search(self, _event=None):
        if self.search_after_id is not None:
            self.after_cancel(self.search_after_id)
        self.search_after_id = self.after(240, self.run_search)

    def run_search(self):
        self.search_after_id = None
        query = self.query_var.get().strip()
        self.search_tree.delete(*self.search_tree.get_children())
        self.search_rows = []
        rows = self.store.results(query, limit=(5000 if query else None))
        if self.hide_suspect_var.get():
            rows = [r for r in rows if not r.get('suspect')]
        self.search_rows = rows
        for i, result in enumerate(rows):
            flagged = result.get('suspect', False)
            self.search_tree.insert('', 'end', iid=str(i),
                                    values=('⚠' if flagged else '', result['episode'],
                                            format_time(result['start']), result['text']),
                                    tags=('suspect',) if flagged else ())
        self.search_count.set((f'検索結果：{len(rows)}件' + ('（上限5000件）' if len(rows) == 5000 else ''))
                              if query else f'全セリフ：{len(rows)}件（検索欄に入力すると絞り込み）')
        self._set_detail('')
        self.adjustment_label.set('未選択')

    def selected_result(self):
        ids = self.search_tree.selection()
        if not ids:
            return None
        try:
            return self.search_rows[int(ids[0])]
        except (ValueError, IndexError):
            return None

    def _set_detail(self, text):
        self.detail['state'] = 'normal'
        self.detail.delete('1.0', 'end')
        self.detail.insert('1.0', text)
        self.detail['state'] = 'disabled'

    def show_detail(self, _event=None):
        item = self.selected_result()
        if item:
            reason = item.get('suspect_reason', '')
            self._set_detail(('⚠ 要確認（自動判定）：' + reason + '\n' if reason else '') + item['context'])
            self.adjustment_label.set(f'{self.store.get_playback_adjustment(item):+.1f}秒（行ごと）')
        else:
            self._set_detail('')
            self.adjustment_label.set('未選択')

    def copy_selected(self):
        item = self.selected_result()
        if not item:
            return
        self.clipboard_clear()
        self.clipboard_append(item['text'])
        self.status_var.set('セリフをクリップボードにコピーしました。')

    def open_selected_srt(self):
        item = self.selected_result()
        if item:
            open_in_default(Path(item['srt']))

    def export_csv(self):
        if not self.search_rows:
            messagebox.showinfo('CSV出力', '先に検索してください。')
            return
        filename = filedialog.asksaveasfilename(defaultextension='.csv', filetypes=[('CSV', '*.csv')],
                                                 initialfile=f'{self.store.project["name"]}_検索結果.csv')
        if not filename:
            return
        with open(filename, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            w.writerow(['話数 / ファイル名', '時刻', 'セリフ', '前後のセリフ', '動画のパス'])
            for x in self.search_rows:
                w.writerow([x['episode'], format_time(x['start'], True), x['text'], x['context'], x['video']])
        self.status_var.set(f'検索結果を保存しました：{filename}')

    def _preroll(self) -> float:
        try:
            return max(0.0, min(5.0, float(self.preroll_var.get())))
        except (ValueError, TypeError):
            return 0.5

    def adjust_playback(self, delta: float):
        item = self.selected_result()
        if not item:
            return
        new = self.store.get_playback_adjustment(item) + delta
        self.store.set_playback_adjustment(item, new)
        self.show_detail()
        self.status_var.set(f'再生位置を{self.store.get_playback_adjustment(item):+.1f}秒に補正しました。再生ボタンで確認できます。')

    def reset_playback(self):
        item = self.selected_result()
        if not item:
            return
        self.store.set_playback_adjustment(item, 0.)
        self.show_detail()
        self.status_var.set('このセリフの再生位置補正を解除しました。')

    def play_selection(self):
        item = self.selected_result()
        if not item:
            messagebox.showinfo('再生', '検索結果から1件選択してください。')
            return
        video = Path(item['video'])
        if not video.is_file():
            messagebox.showerror('動画なし', f'元の動画が見つかりません：\n{video}')
            return
        start = playback_seek_time(item['start'], self._preroll(),
                                   self.store.get_playback_adjustment(item))
        configured = self.player_var.get().strip()
        player = configured or detect_player()
        if player and Path(player).exists():
            name = Path(player).name.lower()
            try:
                cmd = player_command(player, video, start)
            except ValueError as exc:
                messagebox.showwarning('プレーヤー', str(exc))
                return
            try:
                subprocess.Popen(cmd, creationflags=popen_flags(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.status_var.set(f'{video.name} の {format_time(start)} から再生')
                return
            except OSError as e:
                messagebox.showerror('再生エラー', str(e))
                return
        ffmpeg = self.ffmpeg_var.get().strip() or find_ffmpeg(self.exe_var.get().strip())
        if not ffmpeg or not Path(ffmpeg).is_file():
            messagebox.showinfo('再生準備', '該当時刻から再生するには MPC-HC・MPC-BE・mpv・VLC・ffplay のいずれかを設定してください。\n'
                                 'FFmpegを設定すれば、短いプレビュー動画を生成する方法も利用できます。')
            self.nb.select(self.tab_settings)
            return
        self.status_var.set('該当場面の短いプレビューを作成しています…')
        threading.Thread(target=self.make_clip, args=(ffmpeg, video, start), daemon=True).start()

    def make_clip(self, ffmpeg, video, start):
        try:
            import hashlib
            ident = hashlib.sha1((str(video) + str(start)).encode('utf-8')).hexdigest()[:12]
            out_dir = self.store.root / 'previews'
            out_dir.mkdir(parents=True, exist_ok=True)
            output = out_dir / f'clip_{ident}.mkv'
            cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-ss', f'{start:.3f}', '-i', str(video),
                   '-t', '14', '-map', '0:v:0?', '-map', '0:a:0?', '-c', 'copy', '-sn', '-dn', '-avoid_negative_ts', 'make_zero', str(output)]
            done = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
                                  creationflags=popen_flags())
            if done.returncode != 0 or not output.is_file() or not output.stat().st_size:
                raise RuntimeError(done.stderr.decode('utf-8', errors='replace')[-900:])
            self.events.put(('preview_ready', str(output)))
        except Exception as e:
            self.events.put(('preview_error', str(e)))

    def on_close(self):
        if self.is_working():
            if not messagebox.askyesno('終了確認', '文字起こしを中断して終了しますか？\n処理済みのSRTは保存されます。'):
                return
            self.cancel_transcription()
        self.save_settings()
        self.destroy()


def main():
    try:
        app = AnimeLineFinder()
        app.mainloop()
    except Exception:
        # Useful for one-click launch problems, even with --windowed PyInstaller.
        logfile = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'AnimeLineFinder_crash.log'
        logfile.write_text(traceback.format_exc(), encoding='utf-8')
        try:
            messagebox.showerror('起動エラー', f'起動に失敗しました。ログ：\n{logfile}')
        except Exception:
            pass
        raise


if __name__ == '__main__':
    main()
