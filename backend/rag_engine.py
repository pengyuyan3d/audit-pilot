"""ADR-3 混合检索 RAG 引擎：拆分 RRF 召回层 + 0~1 阈值精排层，零外部 Embedding 强依赖。"""

from __future__ import annotations

import hashlib
import io
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jieba
from pypdf import PdfReader
from rank_bm25 import BM25Okapi

from backend.config import DB_PATH
from backend.database import clear_contracts, get_connection
from backend.masking import normalize_account

CONTRACT_NO_RE = re.compile(r"合同编号[：:]\s*([A-Za-z0-9\-]+)")
COUNTERPARTY_RE = re.compile(r"乙方（[^）]*）[：:]\s*([^\r\n]+)")
AMOUNT_RE = re.compile(r"(?:合同含税总金额上限|合同总价款|合同总金额)[^0-9]*([0-9,]+\.\d{2})")
ACCOUNT_RE = re.compile(r"(?:乙方(?:唯一)?指定收款账号)[：:]\s*([0-9\s\-]{16,25})")


def _tokenize_bm25(text: str) -> list[str]:
    tokens = [t.strip().lower() for t in jieba.lcut(text) if t.strip() and not re.fullmatch(r"[\W_]+", t)]
    return tokens or ["empty"]


def _char_ngrams(text: str) -> Counter[str]:
    cleaned = re.sub(r"\s+", "", text.lower())
    grams: list[str] = []
    for n in (2, 3):
        if len(cleaned) >= n:
            grams.extend(cleaned[i : i + n] for i in range(len(cleaned) - n + 1))
    # 加入关键数字/字母实体（如 CT-2026-001、0.5%、50%）
    grams.extend(re.findall(r"[a-z0-9\.\-%]+", text.lower()))
    return Counter(grams)


def _cosine_similarity(v1: dict[str, float], v2: dict[str, float]) -> float:
    if not v1 or not v2:
        return 0.0
    if len(v1) > len(v2):
        v1, v2 = v2, v1
    dot = sum(val * v2.get(k, 0.0) for k, val in v1.items())
    return max(0.0, min(1.0, dot))


def _parse_contract_metadata(raw_text: str, filename: str) -> dict[str, Any]:
    m_no = CONTRACT_NO_RE.search(raw_text)
    m_cp = COUNTERPARTY_RE.search(raw_text)
    m_amt = AMOUNT_RE.search(raw_text)
    m_acc = ACCOUNT_RE.search(raw_text)

    contract_no = m_no.group(1).strip() if m_no else Path(filename).stem.split("_")[0]
    counterparty = m_cp.group(1).strip() if m_cp else ""
    amount = float(m_amt.group(1).replace(",", "")) if m_amt else 0.0
    acct_raw = m_acc.group(1).strip() if m_acc else ""
    return {
        "contract_no": contract_no,
        "counterparty": counterparty,
        "contract_amount": amount,
        "designated_account": acct_raw,
        "designated_account_norm": normalize_account(acct_raw),
    }


def _split_into_clauses(doc_id: str, filename: str, contract_no: str, raw_text: str) -> list[dict[str, Any]]:
    """按 [第X页] 与 ## 第X条 结构化切片，保留精确页码与条款编号。"""
    current_page = 1
    clauses: list[dict[str, Any]] = []
    current_clause_no = "前言"
    current_heading = "合同基本信息"
    buf: list[str] = []

    def _flush() -> None:
        text = "\n".join(buf).strip()
        if not text:
            return
        idx = len(clauses) + 1
        clauses.append(
            {
                "chunk_id": f"{doc_id}-C{idx:02d}",
                "doc_id": doc_id,
                "filename": filename,
                "contract_no": contract_no,
                "page": current_page,
                "clause_no": current_clause_no,
                "heading": current_heading,
                "text": text,
            }
        )

    for line in raw_text.splitlines():
        s = line.strip()
        page_match = re.fullmatch(r"\[第(\d+)页\]", s)
        if page_match:
            _flush()
            buf.clear()
            current_page = int(page_match.group(1))
            continue

        heading_match = re.match(r"^##\s*(第[一二三四五六七八九十\d]+条)\s*(.*)$", s)
        if heading_match:
            _flush()
            buf.clear()
            current_clause_no = heading_match.group(1).strip()
            current_heading = heading_match.group(2).strip() or current_clause_no
            buf.append(f"{current_clause_no} {current_heading}".strip())
            continue

        if s:
            buf.append(s)

    _flush()
    return clauses


def extract_text_from_upload(filename: str, file_bytes: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        reader = PdfReader(io.BytesIO(file_bytes))
        pages = []
        for idx, page in enumerate(reader.pages, start=1):
            pages.append(f"[第{idx}页]\n{page.extract_text() or ''}")
        return "\n\n".join(pages)
    return file_bytes.decode("utf-8", errors="ignore")


class HybridRAGEngine:
    """混合检索引擎：BM25 + 字符 n-gram TF-IDF Dense + RRF 召回 + 0~1 精排打分。"""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self.chunks: list[dict[str, Any]] = []
        self._bm25: BM25Okapi | None = None
        self._idf: dict[str, float] = {}
        self._chunk_vecs: list[dict[str, float]] = []

    def ingest_contract(self, filename: str, raw_text: str) -> dict[str, Any]:
        doc_id = f"DOC-{hashlib.md5(filename.encode('utf-8')).hexdigest()[:8].upper()}"
        meta = _parse_contract_metadata(raw_text, filename)
        clauses = _split_into_clauses(doc_id, filename, meta["contract_no"], raw_text)
        now = datetime.now(timezone.utc).isoformat()

        with get_connection(self.db_path) as conn:
            conn.execute("DELETE FROM contract_clauses WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM contract_documents WHERE doc_id = ?", (doc_id,))
            conn.execute(
                """
                INSERT INTO contract_documents (
                    doc_id, filename, contract_no, counterparty, contract_amount,
                    designated_account, designated_account_norm, raw_text, uploaded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doc_id,
                    filename,
                    meta["contract_no"],
                    meta["counterparty"],
                    meta["contract_amount"],
                    meta["designated_account"],
                    meta["designated_account_norm"],
                    raw_text,
                    now,
                ),
            )
            for c in clauses:
                conn.execute(
                    """
                    INSERT INTO contract_clauses (
                        chunk_id, doc_id, filename, contract_no, page, clause_no, heading, text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        c["chunk_id"],
                        c["doc_id"],
                        c["filename"],
                        c["contract_no"],
                        c["page"],
                        c["clause_no"],
                        c["heading"],
                        c["text"],
                    ),
                )

        self.rebuild_index()
        return {"doc_id": doc_id, "filename": filename, "clauses_count": len(clauses), **meta}

    def ingest_directory(self, directory: Path, reset: bool = True) -> list[dict[str, Any]]:
        if reset:
            clear_contracts(self.db_path)
        results = []
        for path in sorted(directory.glob("*.md")):
            results.append(self.ingest_contract(path.name, path.read_text(encoding="utf-8")))
        return results

    def rebuild_index(self) -> None:
        with get_connection(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM contract_clauses ORDER BY doc_id, chunk_id").fetchall()
        self.chunks = [dict(r) for r in rows]
        if not self.chunks:
            self._bm25 = None
            self._idf = {}
            self._chunk_vecs = []
            return

        # 1. BM25 索引
        tokenized_corpus = [_tokenize_bm25(f"{c['contract_no']} {c['heading']} {c['text']}") for c in self.chunks]
        self._bm25 = BM25Okapi(tokenized_corpus)

        # 2. 局部字符 n-gram TF-IDF 归一化向量索引
        doc_counters = [_char_ngrams(f"{c['contract_no']} {c['heading']} {c['text']}") for c in self.chunks]
        n_docs = len(doc_counters)
        df: Counter[str] = Counter()
        for cnt in doc_counters:
            for term in cnt.keys():
                df[term] += 1
        self._idf = {term: math.log((n_docs + 1) / (freq + 1)) + 1.0 for term, freq in df.items()}

        self._chunk_vecs = []
        for cnt in doc_counters:
            vec = {t: (1.0 + math.log(f)) * self._idf[t] for t, f in cnt.items()}
            norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
            self._chunk_vecs.append({t: v / norm for t, v in vec.items()})

    def _encode_query(self, query: str) -> dict[str, float]:
        cnt = _char_ngrams(query)
        vec = {t: (1.0 + math.log(f)) * self._idf.get(t, 1.0) for t, f in cnt.items() if t in self._idf}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / norm for t, v in vec.items()}

    def search(
        self,
        query: str,
        top_k: int = 4,
        mode: str = "hybrid",
        min_score: float = 0.15,
        contract_no: str | None = None,
    ) -> list[dict[str, Any]]:
        """执行检索：mode='hybrid'（RRF 召回 + 0~1 精排）或 'bm25_only'（用于 A/B 对比评测）。"""
        if not self.chunks:
            self.rebuild_index()
        if not self.chunks or self._bm25 is None:
            return []

        q_tokens = _tokenize_bm25(query)
        bm25_scores = list(self._bm25.get_scores(q_tokens))
        max_bm25 = max(bm25_scores) if bm25_scores and max(bm25_scores) > 0 else 1.0

        q_vec = self._encode_query(query)
        dense_scores = [_cosine_similarity(q_vec, cvec) for cvec in self._chunk_vecs]
        max_dense = max(dense_scores) if dense_scores and max(dense_scores) > 0 else 1.0

        # 排序计算 RRF（k=60）
        bm25_rank = {idx: rank + 1 for rank, idx in enumerate(sorted(range(len(self.chunks)), key=lambda i: bm25_scores[i], reverse=True))}
        dense_rank = {idx: rank + 1 for rank, idx in enumerate(sorted(range(len(self.chunks)), key=lambda i: dense_scores[i], reverse=True))}

        # Stage 1: Recall top-8
        candidates = []
        for idx, chunk in enumerate(self.chunks):
            if contract_no and chunk["contract_no"] != contract_no:
                continue
            b_norm = bm25_scores[idx] / max_bm25 if max_bm25 > 0 else 0.0
            d_norm = dense_scores[idx] / max_dense if max_dense > 0 else 0.0
            if mode == "bm25_only":
                rrf_score = 1.0 / (60.0 + bm25_rank[idx])
                rerank_score = round(b_norm, 4)
            else:
                rrf_score = (1.0 / (60.0 + bm25_rank[idx])) + (1.0 / (60.0 + dense_rank[idx]))
                # Stage 2: 0~1 校准精排分（稠密语义 + BM25 词频 + 精确实体命中加成）
                exact_bonus = 0.0
                if chunk["contract_no"] and chunk["contract_no"].lower() in query.lower():
                    exact_bonus += 0.15
                if chunk["clause_no"] and chunk["clause_no"] in query:
                    exact_bonus += 0.15
                rerank_score = round(min(1.0, 0.50 * d_norm + 0.35 * b_norm + 0.15 * min(1.0, rrf_score * 30.0) + exact_bonus), 4)

            candidates.append(
                {
                    **chunk,
                    "bm25_score": round(float(bm25_scores[idx]), 4),
                    "dense_score": round(float(dense_scores[idx]), 4),
                    "rrf_score": round(float(rrf_score), 5),
                    "relevance_score": rerank_score,
                    "retrieval_mode": mode,
                }
            )

        if mode == "bm25_only":
            candidates.sort(key=lambda x: (x["bm25_score"], x["rrf_score"]), reverse=True)
            filtered = [c for c in candidates if c["bm25_score"] > 0][:top_k]
            return filtered if filtered else candidates[:top_k]

        # Hybrid: 先按 RRF 取 Top-8 召回，再按 0~1 精排分重排并按阈值过滤
        candidates.sort(key=lambda x: x["rrf_score"], reverse=True)
        recall_pool = candidates[:8]
        recall_pool.sort(key=lambda x: x["relevance_score"], reverse=True)
        filtered = [c for c in recall_pool if c["relevance_score"] >= min_score][:top_k]
        return filtered if filtered else recall_pool[:top_k]

    def list_documents(self) -> list[dict[str, Any]]:
        with get_connection(self.db_path) as conn:
            docs = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT d.doc_id, d.filename, d.contract_no, d.counterparty,
                           d.contract_amount, d.designated_account, d.uploaded_at,
                           COUNT(c.chunk_id) AS clause_count
                    FROM contract_documents d
                    LEFT JOIN contract_clauses c ON d.doc_id = c.doc_id
                    GROUP BY d.doc_id
                    ORDER BY d.contract_no
                    """
                ).fetchall()
            ]
        return docs

    def get_document_clauses(self, doc_id: str) -> list[dict[str, Any]]:
        with get_connection(self.db_path) as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM contract_clauses WHERE doc_id = ? ORDER BY chunk_id",
                    (doc_id,),
                ).fetchall()
            ]
