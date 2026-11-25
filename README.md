# Anime Line Finder

アニメの動画を文字起こしして字幕（SRT）を作り、セリフを検索・書き出しできる Windows 向け GUI ツールです。
文字起こしエンジンは **Anime Whisper**（litagin/anime-whisper）と **Qwen3-ASR ＋ ForcedAligner** から選べます。

## 必要なもの
- Windows 10 / 11
- Python 3.10〜3.12（64bit。python.org 版、インストール時に「Add Python to PATH」と Python Launcher を有効に）
- NVIDIA GPU（推奨。無い場合は CPU で動きますがかなり遅くなります）
- 空きディスク：エンジン環境とモデルで 10GB 以上

## インストール
1. このリポジトリを ZIP でダウンロードして展開するか、`git clone` します。
2. `起動.bat` をダブルクリックします。初回は `tkinterdnd2`（ドラッグ＆ドロップ用）を自動でインストールします。
   手動で入れる場合：`py -3 -m pip install -r requirements.txt`
3. アプリの「設定」タブで、使う文字起こしエンジンの実行環境を導入します（初回のみ）。
   同じ内容を次の bat でも実行できます。
   - `AnimeWhisper_導入.bat` … Anime Whisper 用の環境（PyTorch CUDA 12.6、transformers 4.57.3 ほか）
   - `Qwen3ASR_導入.bat` … Qwen3-ASR 用の環境（qwen-asr 0.0.6 ほか。Anime Whisper とは別環境）
   - `VAD_追加導入.bat` … 既存の Anime Whisper 環境に Silero VAD を追加

   環境は `%LOCALAPPDATA%\AnimeLineFinder\` 配下の専用 venv に作られ、本体の Python には影響しません。
   モデル（Anime Whisper 約3GB、Qwen3-ASR 一式 約5GB）は初回の文字起こし時に自動でダウンロードされます。

## 使い方
1. 「作品」を追加し、動画ファイルまたはフォルダを登録します（初回は「サンプル作品」が作られます。名前の変更・削除ができます）。
2. 必要に応じて登場人物名などの「用語」を登録します（読みや別名を登録すると認識結果の補正に使われます）。
3. 「文字起こし」を実行すると、動画と同じフォルダに SRT が作られます。
4. 検索タブでセリフを検索し、該当シーンの再生や CSV 書き出しができます。

詳しい説明・更新履歴は [README_最初にお読みください.txt](README_最初にお読みください.txt) を参照してください。

## EXE 版を作る
`EXE作成.bat` を実行すると PyInstaller で `dist\AnimeLineFinder\AnimeLineFinder.exe` を作ります（フォルダごと配布してください）。

## データの保存場所
作品・用語・設定は `%LOCALAPPDATA%\AnimeLineFinder\` に保存されます（環境変数 `ANIME_LINE_FINDER_DATA` で変更可）。
アンインストールするときはこのフォルダも削除してください。

## ライセンス
MIT License（[LICENSE](LICENSE)）。モデルやライブラリはそれぞれのライセンスに従います。
