# IMPL_PLAN: Grokストリーミング即時再生（--stream）

## 目的
Grokの応答完了を待たず、**生成中に文が確定するたび** SBV2合成→ゲームへ `speak_sequence`/`append` 送信し、
**即時再生**する。SDプロンプトは `[SD_PROMPT_BEGIN/END]` を2状態パースで分離し、**ENDで即送信**（読み上げ到達前）。

## 大方針（回帰ゼロのため）
- 新経路は **`--stream` フラグ ＋ `--llm-backend grok_browser` ＋ `--sbv2-server-url` 指定時のみ**有効。
- 既存（local_openai / SBV2サブプロセス / 非stream）は**一切変更しない**。フラグ無し時は完全に従来動作。
- セッション/キュー/キャンセルは**ゲーム側（ExternalVoicePlayer）が既に実装済み**。Python側で再実装しない。

## 必要材料（調査済み）
| 材料 | 結果 | 出典 |
|---|---|---|
| ゲーム受信transport | 名前付きパイプ `kks_voice_face_events`、1行1JSON、UTF-8 no-BOM | `MainGameVoiceFaceEventBridge/ExternalPipeServer.cs` |
| commandスキーマ | `ExternalVoiceFaceCommand`：type / sessionId / interrupt / items[](audioPath,subtitle,durationSeconds,holdSeconds,deleteAfterPlay) / face / facePreset* / volume / pitch | `ExternalVoiceFaceCommand.cs` |
| type→挙動 | `speak_sequence`→`PlaySequence`（interrupt時 `ClearQueuedItems`+停止→新sessionで#1再生）/ `speak_sequence_append`→`AppendSequence`（session不一致は拒否）/ `stop`→`Stop`（全破棄） | `Plugin.Handlers.cs`(5569,5732,5815) / `ExternalVoicePlayer.cs` |
| 既存イベント送信 | `_send_sequence_line_event`：line_no==1で `speak_sequence`(interrupt=1)、>1で `speak_sequence_append`。sessionId付き。`send_voice_face_event.ps1 -JsonFile` でパイプ送信 | `grok_bridge/tts_event_cli.py` |
| Grokストリーム読み | `send_text` 後、応答DOMをポーリングして文末記号で文確定。実証済み `GrokStreamParser`（SPEAK/COLLECT 2状態＋END欠落ポリシー） | test: `tools/realtime_voice_test/grok_client.py` |
| SBV2サーバー合成 | `_tts_via_http_server`(`/voice`、モデル常駐で高速) | `tts_event_cli.py` |
| SD送信 | `send_a1111_txt2img`。`[SD_PROMPT_BEGIN/END]` 抽出は2状態パースに統合。ENDで即送信 | `core/sd_prompt_bridge.py` |

## 変更点（`--stream` 経路のみ）
1. `grok_bridge/` に `GrokStreamParser` ＋ ストリーム読み関数を移植（test の実証コードを移植）。
2. `tts_event_cli.main()` に streaming 分岐を追加：
   - `sessionId = run_dir.name`
   - `connect → send_text → stream_sentences(on_sentence, on_sd_prompt)`
   - `on_sentence(文)`: 変換辞書を**文単位**で適用（send/display、random_pick_cache を跨いで共有）→ SBV2サーバー合成 → `parts/line_NNN.wav` 書き出し → `_send_sequence_line_event(line_index, interrupt=(line==1))`
   - `on_sd_prompt(prompt)`: SD有効かつ `sd_skip_send` でなければ **別スレッドで** `send_a1111_txt2img`（喋りを止めない）
   - 末尾で全文/行配列を集計し従来の結果JSONを print（worker互換）
3. 新引数: `--stream`、`--sd-unclosed-policy {auto,prompt,speak,discard}`（begin/end tagは既存引数を流用）。

## リスク（要チューニング・既存は無傷）
1. 変換辞書の文単位適用（cache共有）/ 2. SD二重発火（`sd_skip_send`/generate_forever と排他）/ 3. 最終JSONの整合 / 4. パイプ送信が文単位（PowerShell起動/文のレイテンシ床）。

## やらないこと
- 既存の非stream動作・local_openai・SBV2サブプロセス経路の変更。
- ゲーム側プラグインの変更（プロトコルは既存のまま乗る）。

## デプロイ
- 反映は `F:/kks/work/scripts/Deploy-Human2KksPipelineLocal.ps1`（CODEBASE_STATE.md記載）。push/release時のアカウント・名義ルール厳守。
