# IMPL_PLAN: SDプロンプト書き換えルール（ワード→置換／追加）

## 目的
AIが `[SD_PROMPT_BEGIN]`〜`[SD_PROMPT_END]` に出した**SD用プロンプト（英語）**を、A1111へ送る**直前**にルールで書き換える。
- 置換: 例 `20years` → `18years`
- 追加: 例 `beach` が含まれていたら末尾に `blue sky, ocean` を足す
ルールは設定で何個でも登録できる。AIがSDブロックを出さない時は何もしない（現状維持）。

## 大方針（回帰ゼロのため）
- 適用は**1か所だけ**＝`core/sd_prompt_bridge.py` の `build_a1111_txt2img_payload`（`prompt` を組み立てる直前）。
  ここを通る全送信経路（バッチ/ストリーム/generate-forever）が自動で対象になる。
- ルールが空（未登録）なら**完全に従来動作**。既存の `sd_prompt_append_prompt`（常時追加）はそのまま残し、書き換え後に従来どおり結合する。
- 既存の「変換辞書（セリフ用）」とはデータも経路も別物。混ぜない。

## ルール仕様
1ルール = `{enabled: bool, mode: "replace"|"append", from: str, to: str}`
- 登録順に上から適用。
- `enabled=false` または `from` 空 → スキップ。
- `replace`: `from` を大文字小文字無視で探し、見つかった箇所を `to` に置換（全件）。
- `append`: `from` を大文字小文字無視で含むなら、末尾に `, {to}`（既に含まれていれば足さない）。
- 適用対象は **AIのSDプロンプト本文のみ**。`sd_prompt_append_prompt`（常時追加文）は書き換え後に結合（順序: 書き換え済みprompt → append_prompt）。

## 必要材料（調査済み）
| 材料 | 結果 | 出典 |
|---|---|---|
| SDプロンプトの合流点 | `build_a1111_txt2img_payload` が `prompt`＋`append_prompt` を結合（163-167行）。全送信が `send_a1111_txt2img`→ここを通る | `core/sd_prompt_bridge.py` |
| 送信呼び出し3か所 | ①バッチ `main()`(1057) ②ストリーム `_sd_send_worker`(695) ③generate-forever ループ(632) | `tts_event_cli.py` / `workers/pipeline_worker.py` |
| サブプロセス引数 | pipeline_worker が `--sd-prompt-*` を全部CLIで渡す（1819-1868）。tts_event_cli が `_build_arg_parser` で受ける（606-647） | 同上 |
| 設定のリスト項目の型 | `@dataclass AppConfig`。`conversion_dict: list[dict] = field(default_factory=list)` 等が既存パターン | `config/models.py` 145-147 |
| 設定の読み書き | 収集(190-211)→`AppConfig`(366)→`save_config`(519)→`load_config`(745) でリストはそのまま往復 | `controllers/settings_controller.py` |
| GUIのSD設定位置 | SD設定フォーム内「追加プロンプト」(907-910)の近く。表UIは「変換辞書」タブ(`conversion_table` 2299-)が手本 | `gui/main_window.py` |

## 変更点
1. **`config/models.py`**: `sd_prompt_rewrite_rules: list[dict] = field(default_factory=list)` を追加（既存リスト項目と同型）。
2. **`core/sd_prompt_bridge.py`**:
   - `apply_sd_prompt_rewrite_rules(prompt, rules) -> str` を新規追加。
   - `build_a1111_txt2img_payload(..., prompt_rewrite_rules=None)` で `prompt` を最初に書き換え。
   - `send_a1111_txt2img(..., prompt_rewrite_rules=None)` で受けて渡す。
3. **`grok_bridge/tts_event_cli.py`**:
   - `--sd-prompt-rewrite-rules-json` 引数追加（JSON配列文字列）。
   - main() で `json.loads` → list。バッチ(1057)・ストリーム `_sd_send_worker`(695) の `send_a1111_txt2img` に `prompt_rewrite_rules=` を渡す。
4. **`workers/pipeline_worker.py`**:
   - generate-forever の `send_a1111_txt2img`(632) に `prompt_rewrite_rules=self._cfg.sd_prompt_rewrite_rules` を渡す。
   - サブプロセスCLIに `--sd-prompt-rewrite-rules-json json.dumps(...)` を追加（1856付近、`sd_prompt_send_enabled` ブロック内）。
5. **`controllers/settings_controller.py`**: GUI→収集→`AppConfig`、`save_config`、`load_config` に `sd_prompt_rewrite_rules` を追加（`conversion_dict` と同じ要領）。
6. **`gui/main_window.py`**: SD設定タブにルール編集UIを追加（※下の「要決定」参照）。
7. **`config.sample.json`**: `sd_prompt_rewrite_rules: []` を追記。

## 要決定（GUIの作り込み）
- A: SDタブ内に小さな表（有効／種類[置換/追加]／対象ワード／置換・追加文＋行追加/削除）。既存の操作感に近い。**推奨**
- B: JSON貼り付けの小窓のみ（最小・すぐ動く）。
- C: 「変換辞書」タブ並みのフル機能（プリセット保存・検索・並べ替え）。重い。

## リスク
1. 大文字小文字無視の置換は `re.sub(re.escape(from), to, prompt, flags=IGNORECASE)`。`to` 内の `\1` 等は使わない（リテラル化）。
2. ルール順序で結果が変わる（上から適用）。表の行順＝適用順とする。
3. 二重適用の防止: 書き換えは bridge の1か所のみ。GUIプレビューや他経路で二重に掛けない。

## やらないこと
- セリフ用「変換辞書」・既存の常時追加(`sd_prompt_append_prompt`)の挙動変更。
- AIがSDブロックを出さない場合の新規発火（足すだけ＝既存ブロック有り時のみ）。
- ゲーム側プラグインの変更。

## デプロイ
- ローカル反映は `F:/kks/work/scripts/Deploy-Human2KksPipelineLocal.ps1`（`CODEBASE_STATE.md` 記載）。
- push/release は `F:/kks/work/tools/easy_deploy`（アカウント・名義ルール厳守）。
