"""Grok応答ストリームの2状態パーサ（SPEAK / COLLECT）。純ロジック・依存なし。

- SPEAK: 文末記号で文を確定し on_sentence(文) を呼ぶ
- begin_tag 検知 → COLLECT。以降は読まずSDバッファへ収集（境界直前は端数も喋る）
- end_tag 検知 → on_sd_prompt(prompt) を**パース時に即**呼ぶ（再生より先行）→ SPEAK へ戻る
- finish(): 生成終了時。SPEAKなら残り全部喋る。COLLECTのままEND欠落なら unclosed_policy で解決
    - auto: 日本語っぽければ喋る / 英語プロンプトっぽければSD送信 / 空なら無視
    - prompt / speak / discard: 強制
- マーカーがポール跨ぎで分割されても部分一致を保留して誤読/誤収集を防ぐ

ゲーム連携用テキスト本体（human_2_KKS_pipeline）に組み込む共通部品。
test 実装（tools/realtime_voice_test/grok_client.py）と同一ロジック。
"""

from __future__ import annotations

# 文末記号（♪… は文中多用で細切れになるため除外）
SENTENCE_ENDERS = "。！？!?\n"


def split_region(region: str) -> tuple[list[str], str, int]:
    """文末記号で区切る。(完成文リスト, 末尾の未完断片, 完成文が消費した文字数) を返す。"""
    out: list[str] = []
    start = 0
    for i, ch in enumerate(region):
        if ch in SENTENCE_ENDERS:
            seg = region[start : i + 1].strip()
            if seg:
                out.append(seg)
            start = i + 1
    return out, region[start:], start


def _ifind(text: str, tag: str) -> int:
    return text.lower().find(tag.lower())


def partial_tag_holdback(text: str, tag: str) -> int:
    """text の末尾が tag の前方一致（部分マーカー）なら、その文字数を返す（確定保留用）。"""
    maxk = min(len(tag) - 1, len(text))
    low_text = text.lower()
    low_tag = tag.lower()
    for k in range(maxk, 0, -1):
        if low_text[-k:] == low_tag[:k]:
            return k
    return 0


def looks_like_speech(s: str) -> bool:
    """日本語（かな/カナ/漢字）や 。！？ を含めば「喋り」とみなす（SDプロンプトは英語想定）。"""
    for ch in s:
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF or 0x4E00 <= o <= 0x9FFF:
            return True
        if ch in "。！？":
            return True
    return False


class GrokStreamParser:
    def __init__(
        self,
        on_sentence,
        on_sd_prompt=None,
        begin_tag: str = "[SD_PROMPT_BEGIN]",
        end_tag: str = "[SD_PROMPT_END]",
        unclosed_policy: str = "auto",
        logger=None,
    ):
        self.on_sentence = on_sentence
        self.on_sd_prompt = on_sd_prompt
        self.begin = begin_tag or "[SD_PROMPT_BEGIN]"
        self.end = end_tag or "[SD_PROMPT_END]"
        self.policy = (unclosed_policy or "auto").strip().lower()
        self.logger = logger
        self.mode = "speak"
        self.consumed = 0
        self.sd_buf = ""

    def _emit(self, region: str, flush_all: bool) -> int:
        sents, tail, used = split_region(region)
        for s in sents:
            self.on_sentence(s)
        if flush_all:
            t = tail.strip()
            if t:
                self.on_sentence(t)
            return len(region)
        return used

    def _fire_sd(self, buf: str) -> None:
        p = (buf or "").strip()
        if p and self.on_sd_prompt:
            self.on_sd_prompt(p)

    def _resolve_dangling(self) -> None:
        buf = self.sd_buf.strip()
        self.sd_buf = ""
        if not buf:
            return
        policy = self.policy
        if policy == "auto":
            policy = "speak" if looks_like_speech(buf) else "prompt"
        if self.logger:
            self.logger.warning("[stream] SD_END missing -> policy=%s len=%d", policy, len(buf))
        if policy == "prompt":
            self._fire_sd(buf)
        elif policy == "speak":
            self._emit(buf, flush_all=True)
        # discard: 何もしない

    def feed(self, full_text: str, final: bool = False) -> None:
        while True:
            pending = full_text[self.consumed :]
            if not pending:
                break
            if self.mode == "speak":
                p = _ifind(pending, self.begin)
                if p >= 0:
                    self._emit(pending[:p], flush_all=True)  # 境界＝端数も喋る
                    self.consumed += p + len(self.begin)
                    self.mode = "collect"
                    self.sd_buf = ""
                    continue
                if final:
                    self._emit(pending, flush_all=True)
                    self.consumed = len(full_text)
                    break
                hold = partial_tag_holdback(pending, self.begin)
                speakable = pending[: len(pending) - hold] if hold else pending
                self.consumed += self._emit(speakable, flush_all=False)
                break
            else:  # collect
                q = _ifind(pending, self.end)
                if q >= 0:
                    self.sd_buf += pending[:q]
                    self.consumed += q + len(self.end)
                    self._fire_sd(self.sd_buf)  # ★END検知で即送信（パース時＝再生より先行）
                    self.sd_buf = ""
                    self.mode = "speak"
                    continue
                if final:
                    self.sd_buf += pending
                    self.consumed = len(full_text)
                    self._resolve_dangling()
                    break
                hold = partial_tag_holdback(pending, self.end)
                take = pending[: len(pending) - hold] if hold else pending
                self.sd_buf += take
                self.consumed += len(take)
                break

    def finish(self, full_text: str) -> None:
        self.feed(full_text, final=True)
        # collect中に非finalで全消費してpendingが空のまま終わった場合の取りこぼし救済
        if self.mode == "collect" and self.sd_buf.strip():
            self._resolve_dangling()
