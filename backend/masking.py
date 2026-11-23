"""ADR-1 数据脱敏与流式还原网关模块。

核心不变量：
1. 脱敏仅发生在请求外发 LLM 之前；本地 SQL / RAG / 交叉核对始终运行在原文上。
2. 实体归一化：银行账号去除空格/横杠后映射，保证 `6222 0210 ...` 与 `62220210...` 映射到同一个 `[BANK_ACCT_k]`，消除格式差异造成的假阳性误报。
3. 流式还原状态机：跨 chunk 缓冲 `[...]` 占位符，防止 SSE 吐字时闪烁出未还原占位符。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


BANK_ACCT_RE = re.compile(r"(?<!\d)(?:\d[\s-]?){15,21}\d(?!\d)")
TAX_ID_RE = re.compile(r"\b9[1239][0-9A-Z]{16}\b")
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d[\s-]?\d{4}[\s-]?\d{4}(?!\d)")
COMPANY_RE = re.compile(
    r"[\u4e00-\u9fa5]{2,18}(?:有限责任公司|股份有限公司|集团有限公司|有限公司|加工厂|事务所|中心)"
)
PLACEHOLDER_RE = re.compile(r"\[(?:BANK_ACCT|TAX_ID|PHONE|ENTITY)_\d+\]")


def normalize_account(raw: str) -> str:
    return re.sub(r"[\s-]+", "", raw or "").strip()


def normalize_phone(raw: str) -> str:
    return re.sub(r"[\s-]+", "", raw or "").strip()


@dataclass
class MaskingContext:
    """单次请求的脱敏上下文：维护归一化实体到占位符的双向映射。"""

    known_entities: list[str] = field(default_factory=list)
    _canonical_to_token: dict[str, str] = field(default_factory=dict)
    _token_to_display: dict[str, str] = field(default_factory=dict)
    _counters: dict[str, int] = field(
        default_factory=lambda: {"BANK_ACCT": 0, "TAX_ID": 0, "PHONE": 0, "ENTITY": 0}
    )
    _records: list[dict[str, str]] = field(default_factory=list)

    def _register(self, category: str, canonical: str, display: str) -> str:
        key = f"{category}:{canonical}"
        if key not in self._canonical_to_token:
            self._counters[category] += 1
            token = f"[{category}_{self._counters[category]}]"
            self._canonical_to_token[key] = token
            self._token_to_display[token] = display
            self._records.append(
                {
                    "category": category,
                    "placeholder": token,
                    "normalized": canonical,
                    "original": display,
                }
            )
        return self._canonical_to_token[key]

    def mask(self, text: str) -> str:
        if not text:
            return text
        out = text

        # 1. 优先脱敏税号（避免 18 位含数字字符被其他正则误伤）
        def _sub_tax(m: re.Match[str]) -> str:
            raw = m.group(0)
            return self._register("TAX_ID", raw.upper(), raw)

        out = TAX_ID_RE.sub(_sub_tax, out)

        # 2. 银行账号（归一化去空格/横杠后映射，保证合同与流水格式统一）
        def _sub_bank(m: re.Match[str]) -> str:
            raw = m.group(0)
            digits = normalize_account(raw)
            if not (16 <= len(digits) <= 22):
                return raw
            return self._register("BANK_ACCT", digits, digits)

        out = BANK_ACCT_RE.sub(_sub_bank, out)

        # 3. 手机号
        def _sub_phone(m: re.Match[str]) -> str:
            raw = m.group(0)
            digits = normalize_phone(raw)
            if len(digits) != 11:
                return raw
            return self._register("PHONE", digits, digits)

        out = PHONE_RE.sub(_sub_phone, out)

        # 4. 已知企业主体名（按长度降序优先匹配，避免子串残留）
        for ent in sorted({e.strip() for e in self.known_entities if e and len(e.strip()) >= 3}, key=len, reverse=True):
            if ent in out:
                token = self._register("ENTITY", ent, ent)
                out = out.replace(ent, token)

        # 5. 正则兜底匹配其余企业主体名
        def _sub_company(m: re.Match[str]) -> str:
            raw = m.group(0)
            return self._register("ENTITY", raw, raw)

        out = COMPANY_RE.sub(_sub_company, out)
        return out

    def unmask(self, text: str) -> str:
        if not text or not self._token_to_display:
            return text
        return PLACEHOLDER_RE.sub(lambda m: self._token_to_display.get(m.group(0), m.group(0)), text)

    def summary(self) -> dict:
        return {
            "total_masked_entities": len(self._records),
            "counts_by_category": dict(self._counters),
            "mappings": list(self._records),
        }

    def verify_zero_leakage(self, masked_text: str) -> list[str]:
        """检验外发文本中是否存在任何原始敏感信息泄露。"""
        leaks: list[str] = []
        norm_text = normalize_account(masked_text)
        for rec in self._records:
            cat = rec["category"]
            orig = rec["original"]
            norm = rec["normalized"]
            if orig in masked_text:
                leaks.append(f"{cat}:{orig}")
            elif cat in ("BANK_ACCT", "PHONE") and norm in norm_text:
                leaks.append(f"{cat}:{norm}")
        return leaks


class StreamingUnmasker:
    """SSE 流式占位符还原状态机：跨 chunk 缓冲 `[...]`，防止前端闪烁占位符。"""

    MAX_TOKEN_LEN = 24

    def __init__(self, ctx: MaskingContext) -> None:
        self.ctx = ctx
        self._buf = ""

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._buf += chunk
        output_parts: list[str] = []

        while self._buf:
            idx = self._buf.find("[")
            if idx == -1:
                output_parts.append(self._buf)
                self._buf = ""
                break
            if idx > 0:
                output_parts.append(self._buf[:idx])
                self._buf = self._buf[idx:]

            close_idx = self._buf.find("]")
            if close_idx != -1:
                candidate = self._buf[: close_idx + 1]
                output_parts.append(self.ctx.unmask(candidate))
                self._buf = self._buf[close_idx + 1 :]
            else:
                # 尚未收到 `]`：如果长度超过占位符最大可能长度，视为普通文本输出
                if len(self._buf) > self.MAX_TOKEN_LEN:
                    output_parts.append(self._buf[0])
                    self._buf = self._buf[1:]
                else:
                    break

        return "".join(output_parts)

    def flush(self) -> str:
        if not self._buf:
            return ""
        rest = self.ctx.unmask(self._buf)
        self._buf = ""
        return rest
