# human_2_KKS_pipeline

音声入力または外部テキストを受け取り、次の流れで処理するGUIツールです。

1. FasterWhisperで文字起こし
2. Grokへ送信（または手動テキスト）
3. SBV2で音声生成
4. KKSへ音声/表情イベント送信（未起動時はGUI再生）

---

## 1. できること

- マイク音声の常時監視と自動録音
- FasterWhisperサーバーを使った文字起こし
- Grok連携（Selenium / Chromeデバッグ接続）
- SBV2（Style-Bert-VITS2）連携
- KKSへの音声+表情イベント送信
- 字幕送信
- 外部HTTPからの手動テキスト投入
- 新テキスト割り込み（再生中でも、新しい応答が完成した瞬間に差し替え。完成までは現在の音声を継続）
- 文字変換辞書
  - 文字起こし後（Grok送信用 / 表示用を分離）
  - SBV2前（送信用 / 表示用を分離）
- テストタブ
  - FasterWhisper押し録音テスト
  - FasterWhisper手入力変換テスト
  - SBV2単体テスト（KKS送信 / GUI再生）

---

## 2. 必要環境

- Windows 10/11
- ネット接続（初回 `setup.bat` でPython/ライブラリ取得）
- Chrome（Selenium連携を使う場合）
- SBV2環境（SBV2連携を使う場合）
- KKSブリッジ受け側（音声イベント送信を使う場合）

---

## 3. 初回インストール（配布版ユーザー向け）

最初だけ、次の順で実行してください。

1. `setup.bat` を実行する  
   必要なPythonとライブラリの準備を自動で行います。
2. `launch.bat` を実行する  
   GUIが起動します。

2回目以降は `launch.bat` だけで起動できます。  
`setup.bat` は、起動できなくなったときの再セットアップ用です。

### 前の版から引き継いで使う場合

1. 新版を別フォルダに展開
2. 旧版の `config.json` を新版フォルダへコピー
3. `launch.bat` を実行
4. 起動後に必要な項目だけ確認して保存

※ 旧 `config.json` はそのまま読み込めます。

---

## 4. 起動方法

### 標準起動（配布向け）

- `launch.bat`

### venv起動（開発者向け）

- `launch_me.bat`
- `venv\Scripts\python.exe` を使います（`venv` が無いと起動しません）

### エントリポイント（CLI）

- `main.py gui`
- `main.py transcribe-server`
- `main.py transcribe-one`
- `main.py tts-event`

補助互換スクリプト（旧呼び出し互換）:

- `human_2_KKS_pipeline.py`（GUI）
- `run_transcribe_server.py`
- `run_transcribe_one_wav.py`
- `run_grok_tts_event.py`

### FasterWhisper の実行先

設定画面の「FW実行先」で次のどちらかを明示的に選びます。自動切替はしません。

- `ローカルFW`: 既存録音が保存したWAVを、このPCのFasterWhisperへ渡します。
- `サブPC RTFW LAN`: 同じWAVをサブPCへ送り、確定結果だけを既存パイプラインへ渡します。このPCではFasterWhisperを起動しません。

入力デバイスは録音設定タブの既存欄だけを使います。VR PTT、音声ゲート、FWテストが作ったWAVはすべて同じbackend dispatchを通ります。RTFW欄の「接続・認証確認」は経路確認だけで、独自録音は開始しません。

共有トークンは `J:\tools\api-scripts\runtime\.env` の `RTFW_LAN_TOKEN` を読みます。設定ファイルやログには保存しません。経路診断は `J:\tools\api-scripts\runtime\data\rtfw_lan_client\route.jsonl`（送信側）と、サブPCの `Z:\tools\rtfw_remote_worker\logs\route.jsonl`（受信・推論側）を確認してください。

---

## 5. GUIの使い方（重要）

### 録音設定タブ

- 入力デバイス、閾値、無音秒数、最小録音秒数を調整します。
- 閾値は「より小さい値（例: `-40`）」ほど小声を拾いやすくなります。

### パイプライン設定タブ

- FasterWhisper / SBV2 / KKS送信先 / 外部受信 / 字幕送信を設定します。
- `source_mode`
  - `mic`: マイク入力のみ
  - `external`: 外部テキストのみ
  - `both`: 両方

### テストタブ（重要）

#### FasterWhisper テスト

- `押して話す（離して判定）`
  - 押している間だけ録音
  - 離した瞬間に文字起こし実行
- テスト中は本録音が一時停止し、誤送信を防ぎます。
- `入力テキストでテスト`
  - 文字起こしを使わず、手入力テキストで変換結果を確認できます。
- 結果表示は3列:
  - 原文
  - 変換後（Grok送信用）
  - 変換後（表示用）
- `送信用`欄の右側ボタンで、そのテキストを手動送信できます。

#### SBV2 テスト

- 入力テキストをSBV2で音声生成
- 表情指定:
  - `現在表情維持` ON: face固定しない
  - OFF: 指定face値を送信
- 音量スライダーでテスト再生音量を調整
- 結果表示は3列:
  - 原文
  - 変換後（SBV2送信用）
  - 変換後（表示用）
- 実行時挙動:
  - KKS送信先が利用可能ならKKSへ送信
  - 利用不可なら自動でGUIローカル再生

### 変換辞書タブ（SBV2前変換）

- 列:
  - `変換前`
  - `SBV2送信用`
  - `表示用`
  - `表示適用`（チェック）
- `SBV2送信用`は複数候補に対応（ランダム1件）:
  - `候補A|候補B|候補C`
  - 複数行
  - JSON配列 `["候補A","候補B"]`

### 文字起こし変換タブ（Grok前変換）

- FasterWhisper結果に対して適用されます。
- 列:
  - `変換前`
  - `Grok送信用`
  - `表示用`
  - `表示適用`
- 送信用と表示用を分離可能です。

### Seleniumタブ

- Chromeデバッグポート（通常 `9222`）で接続
- プロファイル選択して既存ログイン状態を利用
- `Selenium接続` / `Grokを開く（テスト）` で確認

### フィルタータブ

- 除外フレーズを行単位で登録
- 一致したテキストは後段処理をスキップ

---

## 6. `config.json` 互換性（重要）

以前の `config.json` はそのまま再利用できます。

- 同じフォルダに旧 `config.json` を置いて起動すれば読み込みます。
- 新しいキーが無い場合は既定値で補完されます。
- 旧変換辞書形式（`to` / `to_grok`）も後方互換で読み込みます。
- 保存時に最新形式へ寄せられます（既存値は維持）。

推奨手順:

1. 新版を展開
2. 旧版の `config.json` を新版フォルダへコピー
3. `launch.bat` で起動
4. 必要ならGUIで微調整して保存

---

## 7. 外部テキスト投入API

`source_mode=external` または `both` のとき、外部HTTP受信が有効です。

- 既定URL: `http://127.0.0.1:18767/manual-text`
- メソッド: `POST`
- JSON:

```json
{
  "text": "送信したいテキスト",
  "event_id": "任意の重複防止ID",
  "source": "external"
}
```

---

## 8. よくあるトラブル

### `No module named ...`

- `setup.bat` を再実行してください。
- venv起動時は `venv` 側に同じ依存が必要です。

### Selenium接続失敗（9222関連）

- Chromeがデバッグポートで起動しているか確認
- GUIのSeleniumタブでポート/プロファイルを合わせる
- Chrome/Driverのメジャーバージョン差異がないか確認

### `model_assets not found`

- SBV2ルート設定が誤っています。
- `sbv2_root/model_assets` が存在するパスを指定してください。

### SBV2接続拒否（WinError 10061）

- SBV2サーバー未起動、またはURL不一致です。
- `SBV2サーバーURL` と自動起動設定を確認してください。

---

## 9. 主なファイル

- `main.py`: 司令塔エントリポイント
- `orchestrator/router.py`: サブコマンドルーティング
- `gui/main_window.py`: GUI本体
- `workers/pipeline_worker.py`: 監視/実行ワーカー
- `controllers/settings_controller.py`: 設定入出力
- `config.sample.json`: 設定テンプレート
- `setup.bat`: 依存セットアップ
- `launch.bat`: 標準起動

---

## 10. 更新時の運用メモ（配布者向け）

- リリースはタグ単位（例: `v1.0.0` -> `v1.1.0`）
- リリース本文に比較URLを必ず記載:
  - `https://github.com/canon64/human-2-kks-pipeline/compare/v1.0.0...v1.1.0`
- ユーザーには「新版展開 + 旧 `config.json` 引き継ぎ」を案内
