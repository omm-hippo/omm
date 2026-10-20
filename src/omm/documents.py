"""Local document store, bounded extraction and lexical retrieval. No network calls."""
from __future__ import annotations

import base64
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sqlite3
import unicodedata
import uuid

from omm import config

MAX_UPLOAD = 6 * 1024 * 1024
MAX_TEXT = 500_000
MAX_DOCUMENTS = 100
NO_EVIDENCE = "등록된 문서에서 질문의 근거를 찾지 못했어요. 질문에 문서의 핵심 단어를 포함해 보세요."


def terms(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+|[가-힣]+", unicodedata.normalize("NFKC", text).lower())
    result = []
    for word in words:
        if len(word) < 2 or word in {"the", "and", "for", "with", "what", "which", "this", "that", "are", "from", "does", "how", "문서", "무엇", "어떻게", "알려주세요"}:
            continue
        if re.fullmatch(r"[가-힣]+", word):
            result.extend(word[i:i + 2] for i in range(len(word) - 1))
        else:
            result.append(word)
    return result


def extract(name: str, content: bytes) -> list[tuple[int, str]]:
    suffix = Path(name).suffix.lower()
    if not content or len(content) > MAX_UPLOAD:
        raise ValueError("비어 있거나 6 MiB를 넘는 문서는 등록할 수 없어요.")
    if suffix not in {".pdf", ".txt", ".md", ".markdown"}:
        raise ValueError("텍스트 PDF, TXT, Markdown만 지원해요. 스캔 PDF와 HWP는 아직 지원하지 않아요.")
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ValueError("PDF를 읽으려면 pypdf 패키지가 필요해요. omm을 다시 설치하거나 업데이트한 뒤 시도해 주세요.") from None
        try:
            reader = PdfReader(io.BytesIO(content), strict=True)
            if reader.is_encrypted:
                raise ValueError("암호가 걸린 PDF는 등록할 수 없어요.")
            if len(reader.pages) > 100:
                raise ValueError("PDF는 100쪽까지 등록할 수 있어요.")
            pages = []
            length = 0
            for number, page in enumerate(reader.pages, 1):
                stream = page.get_contents()
                if stream is not None and len(stream.get_data()) > 4 * 1024 * 1024:
                    raise ValueError("PDF 페이지의 처리 크기 제한을 넘었어요.")
                text = page.extract_text() or ""
                length += len(text)
                if length > MAX_TEXT:
                    raise ValueError("문서는 추출된 텍스트 50만 자까지 지원해요.")
                if text.strip():
                    pages.append((number, text))
            if not pages:
                raise ValueError("읽을 수 있는 텍스트가 없어요. 스캔 PDF의 OCR은 아직 지원하지 않아요.")
        except ValueError:
            raise
        except Exception:
            raise ValueError("PDF를 읽지 못했어요. 손상되지 않은 텍스트 PDF인지 확인해 주세요.") from None
    else:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeError:
            raise ValueError("TXT와 Markdown은 UTF-8 파일로 저장해 주세요.") from None
        if "\x00" in text or not text.strip() or len(text) > MAX_TEXT:
            raise ValueError("읽을 수 있는 텍스트가 없거나 50만 자 제한을 넘었어요.")
        pages = [(1, text)]
    return pages


def chunks(pages):
    # Store exact substrings, including line breaks: displayed quotations can
    # be compared with the extracted original rather than model paraphrases.
    for page, text in pages:
        start = 0
        while start < len(text):
            end = min(start + 900, len(text))
            if end < len(text):
                boundary = text.rfind("\n", start + 450, end)
                if boundary > start:
                    end = boundary + 1
            part = text[start:end]
            if part.strip():
                yield page, start, end, part
            if end == len(text):
                break
            start = max(start + 1, end - 100)


class DocumentStore:
    @contextmanager
    def connection(self):
        root = config.OMM_HOME / "documents"
        if root.is_symlink() or config.OMM_HOME.is_symlink():
            raise ValueError("문서 저장 폴더의 연결 경로를 확인해 주세요.")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / "library.sqlite3"
        if any(p.is_symlink() for p in (path, root / "library.sqlite3-journal", root / "library.sqlite3-wal", root / "library.sqlite3-shm")):
            raise ValueError("문서 저장 파일의 연결 경로를 확인해 주세요.")
        db = sqlite3.connect(path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                    sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                    pages INTEGER NOT NULL, imported_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chunks (
                    document_id TEXT NOT NULL, page INTEGER NOT NULL,
                    start INTEGER NOT NULL, end INTEGER NOT NULL, text TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS chunks_document ON chunks(document_id);
                CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL);
                INSERT OR IGNORE INTO state VALUES(1,0);
                CREATE TABLE IF NOT EXISTS answers (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);
            ''')
            with db:
                yield db
        finally:
            db.close()

    def list(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT d.*, (SELECT COUNT(*) FROM chunks WHERE document_id=d.id) AS chunks FROM documents d ORDER BY name")]

    def import_document(self, name: str, content: bytes, *, replace=False):
        if not isinstance(name, str) or not 1 <= len(name) <= 180 or Path(name).name != name or any(ord(c) < 32 for c in name) or "\\" in name:
            raise ValueError("문서 파일 이름을 확인해 주세요.")
        pages = extract(name, content)
        pieces = list(chunks(pages))
        digest = hashlib.sha256(content).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM documents WHERE name=?", (name,)).fetchone()
            if old and old["sha256"] == digest:
                return {"id": old["id"], "name": name, "unchanged": True}
            if old and not replace:
                raise ValueError("같은 이름의 문서가 있어요. 교체를 선택해 주세요.")
            if not old and db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] >= MAX_DOCUMENTS:
                raise ValueError("문서는 100개까지 등록할 수 있어요.")
            total = db.execute("SELECT COALESCE(SUM(LENGTH(text)),0) FROM chunks WHERE document_id != ?", (old["id"] if old else "",)).fetchone()[0]
            if total + sum(len(row[3]) for row in pieces) > 2_000_000:
                raise ValueError("전체 문서의 검색 텍스트는 200만 자까지 지원해요. 기존 문서를 정리해 주세요.")
            key = old["id"] if old else str(uuid.uuid4())
            db.execute("DELETE FROM chunks WHERE document_id=?", (key,))
            db.execute("INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?)", (key, name, digest, len(content), max(p for p, _ in pages), datetime.now(timezone.utc).isoformat()))
            db.executemany("INSERT INTO chunks VALUES(?,?,?,?,?)", [(key, *row) for row in pieces])
            self._changed(db)
        return {"id": key, "name": name, "chunks": len(pieces), "sha256": digest}

    @staticmethod
    def _changed(db):
        db.execute("UPDATE state SET revision=revision+1 WHERE id=1")
        # A removed source must not survive in cached answer quotations.
        db.execute("DELETE FROM answers")

    def delete(self, key):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM documents WHERE id=?", (key,)).fetchone():
                raise ValueError("문서를 찾을 수 없어요. 목록을 새로고침해 주세요.")
            db.execute("DELETE FROM chunks WHERE document_id=?", (key,))
            db.execute("DELETE FROM documents WHERE id=?", (key,))
            self._changed(db)
        return {"removed": True, "id": key}

    def search(self, question: str, ids: list[str]):
        query = set(terms(question))
        with self.connection() as db:
            revision = db.execute("SELECT revision FROM state WHERE id=1").fetchone()[0]
            if not ids or len(ids) > 20 or len(set(ids)) != len(ids):
                raise ValueError("질문할 문서를 1~20개 선택해 주세요.")
            known = {row[0] for row in db.execute("SELECT id FROM documents")}
            if not set(ids) <= known:
                raise ValueError("선택한 문서가 바뀌었어요. 문서 목록을 새로고침해 주세요.")
            placeholders = ",".join("?" for _ in ids)
            rows = [dict(r) for r in db.execute(f"SELECT c.*, d.name, d.sha256 FROM chunks c JOIN documents d ON d.id=c.document_id WHERE d.id IN ({placeholders})", ids)]
        if not query:
            return revision, []
        counts = [Counter(terms(r["text"])) for r in rows]
        frequencies = Counter(term for count in counts for term in query if term in count)
        average = sum(sum(count.values()) for count in counts) / max(1, len(counts))
        scored = []
        for row, count in zip(rows, counts):
            hits = query & count.keys()
            # One accidental bigram is too weak a basis for answering a long
            # Korean question. Lexical matches are not semantic confidence.
            if len(hits) < min(2, len(query)):
                continue
            score = sum(math.log(1 + (len(rows) - frequencies[t] + .5) / (frequencies[t] + .5)) * count[t] * 2.2 / (count[t] + 1.2 * (.25 + .75 * sum(count.values()) / max(1, average))) for t in hits)
            scored.append((score, row))
        scored.sort(key=lambda x: (-x[0], x[1]["name"], x[1]["page"], x[1]["start"]))
        sources = []
        for _, row in scored:
            if any(s["document_id"] == row["document_id"] and s["page"] == row["page"] and abs(s["start"] - row["start"]) < 450 for s in sources):
                continue
            sources.append({**row, "citation": len(sources) + 1})
            if len(sources) == 3:
                break
        return revision, sources

    def save_answer(self, answer, revision):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if revision != db.execute("SELECT revision FROM state WHERE id=1").fetchone()[0]:
                raise ValueError("답변 중 문서가 변경됐어요. 최신 문서로 다시 질문해 주세요.")
            db.execute("INSERT INTO answers VALUES(?,?,?,?)", (answer["id"], revision, answer["created_at"], json.dumps(answer, ensure_ascii=False)))
            db.execute("DELETE FROM answers WHERE id NOT IN (SELECT id FROM answers ORDER BY created_at DESC LIMIT 30)")
        return answer

    def answers(self):
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM answers ORDER BY created_at DESC LIMIT 30")]


def import_request(request):
    try:
        content = base64.b64decode(request["content"], validate=True)
    except (ValueError, TypeError):
        raise ValueError("파일 전송 내용을 확인하지 못했어요.") from None
    return DocumentStore().import_document(request["filename"], content, replace=request.get("replace") is True)
