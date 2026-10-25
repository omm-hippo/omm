"""Local document store, bounded extraction and hybrid (semantic + lexical) retrieval.

Embeddings are requested only from a loopback Ollama origin; nothing else leaves the machine."""
from __future__ import annotations

import base64
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import io
import json
import math
from pathlib import Path
import re
import sqlite3
import unicodedata
import uuid
from xml.etree import ElementTree
import zipfile

from omm import config, document_embeddings

MAX_UPLOAD = 6 * 1024 * 1024
MAX_TEXT = 500_000
MAX_DOCUMENTS = 100
# Recorded on every indexed document. A search over chunks built by another
# retrieval method or chunker would silently mix incompatible indexes.
RETRIEVAL_METHOD = "local_lexical_v1"
CHUNKING_VERSION = "chunk900_overlap100_v1"
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
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError("텍스트 PDF, TXT, Markdown, HTML, HWPX만 지원해요. 스캔 PDF와 HWP(한글 97 형식)는 아직 지원하지 않아요.")
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
    elif suffix in {".html", ".htm"}:
        pages = [(1, _checked(html_text(content), "HTML"))]
    elif suffix == ".hwpx":
        pages = [(1, _checked(hwpx_text(content), "HWPX"))]
    else:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeError:
            raise ValueError("TXT와 Markdown은 UTF-8 파일로 저장해 주세요.") from None
        if "\x00" in text or not text.strip() or len(text) > MAX_TEXT:
            raise ValueError("읽을 수 있는 텍스트가 없거나 50만 자 제한을 넘었어요.")
        pages = [(1, text)]
    return pages


def index_current(row) -> bool:
    return row["retrieval"] == RETRIEVAL_METHOD and row["chunking"] == CHUNKING_VERSION


SUPPORTED_SUFFIXES = {".pdf", ".txt", ".md", ".markdown", ".html", ".htm", ".hwpx"}
_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header", "footer",
               "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "dt", "dd", "hr", "main", "nav", "aside"}
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "object"}


def _checked(text: str, kind: str) -> str:
    text = text.strip("\n")
    if "\x00" in text or not text.strip():
        raise ValueError(f"{kind} 문서에서 읽을 수 있는 본문 텍스트를 찾지 못했어요.")
    if len(text) > MAX_TEXT:
        raise ValueError("문서는 추출된 텍스트 50만 자까지 지원해요.")
    return text


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0
        self.size = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self.skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.size += len(data)
            if self.size > MAX_TEXT * 4:
                raise ValueError("문서는 추출된 텍스트 50만 자까지 지원해요.")
            self.parts.append(data)


def _decode_html(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeError:
        pass
    declared = re.search(rb"""<meta[^>]+charset\s*=\s*["']?([A-Za-z0-9_-]{2,20})""", content[:4096], re.I)
    if declared:
        try:
            return content.decode(declared.group(1).decode("ascii"))
        except (LookupError, UnicodeError):
            pass
    raise ValueError("HTML 문서의 글자 인코딩을 확인하지 못했어요. UTF-8로 저장해 주세요.")


def html_text(content: bytes) -> str:
    parser = _HTMLText()
    parser.feed(_decode_html(content))
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def hwpx_text(content: bytes) -> str:
    """Body text of an HWPX (OWPML zip) document: Contents/section*.xml paragraphs."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise ValueError("HWPX 파일을 열지 못했어요. 한글에서 HWPX로 다시 저장해 주세요.") from None
    with archive:
        sections = sorted((info for info in archive.infolist() if re.fullmatch(r"Contents/section(\d+)\.xml", info.filename)),
                          key=lambda info: int(re.search(r"\d+", info.filename).group()))
        if not sections:
            raise ValueError("HWPX 본문(Contents/section*.xml)을 찾지 못했어요. HWP(한글 97) 파일은 아직 지원하지 않아요.")
        if len(sections) > 200 or sum(info.file_size for info in sections) > 40 * 1024 * 1024:
            raise ValueError("HWPX 본문이 처리 크기 제한을 넘었어요.")
        paragraphs = []
        for info in sections:
            with archive.open(info) as stream:
                data = stream.read(40 * 1024 * 1024 + 1)
            if len(data) > 40 * 1024 * 1024:
                raise ValueError("HWPX 본문이 처리 크기 제한을 넘었어요.")
            # OWPML never needs a DTD; refusing one rules out entity expansion.
            if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
                raise ValueError("HWPX 본문 형식을 확인하지 못했어요.")
            try:
                root = ElementTree.fromstring(data)
            except ElementTree.ParseError:
                raise ValueError("HWPX 본문을 읽지 못했어요. 손상되지 않은 파일인지 확인해 주세요.") from None
            for paragraph in root.iter():
                if _local(paragraph.tag) != "p":
                    continue
                # Only this paragraph's own runs: table cells hold nested paragraphs.
                text = "".join("".join(t.itertext()) for run in paragraph if _local(run.tag) == "run"
                               for t in run if _local(t.tag) == "t")
                if text.strip():
                    paragraphs.append(text)
    return "\n".join(paragraphs)


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


DEFAULT_COLLECTION = "default"
DEFAULT_COLLECTION_NAME = "기본"
# Retrieval methods reported with every search result.
HYBRID = "hybrid"
LEXICAL = "lexical"
# bge-m3 cosine similarity measured on small Korean HR/expense/ops documents
# (2026-10-24): related chunks scored 0.58-0.76, same-domain but wrong chunks
# 0.44-0.51, unrelated questions 0.20-0.27. A chunk must clear the floor and
# stay close to the best chunk; otherwise semantic search adds no evidence.
# Tuned on a few documents only; other embedding models may need other values.
SEMANTIC_MIN_SIMILARITY = 0.5
SEMANTIC_MARGIN = 0.05
RRF_K = 60
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")

_DOCUMENTS_TABLE = '''CREATE TABLE {table} (
    id TEXT PRIMARY KEY, collection_id TEXT NOT NULL DEFAULT 'default', name TEXT NOT NULL,
    sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
    pages INTEGER NOT NULL, imported_at TEXT NOT NULL,
    retrieval TEXT NOT NULL DEFAULT 'local_lexical_v1',
    chunking TEXT NOT NULL DEFAULT 'chunk900_overlap100_v1',
    embedding_model TEXT, embedding_dim INTEGER,
    UNIQUE(collection_id, name))'''
_DOCUMENT_COLUMNS = {"id", "collection_id", "name", "sha256", "size_bytes", "pages", "imported_at",
                     "retrieval", "chunking", "embedding_model", "embedding_dim"}


def _embedder(model: str, base_url: str):
    return document_embeddings.OllamaEmbedder(model, base_url)


def semantic_state(row, model: str) -> str:
    """ready, missing (never embedded), stale (other embedding model) or reregister (lexical index outdated)."""
    if not index_current(row):
        return "reregister"
    if not row["embedding_model"]:
        return "missing"
    if row["embedding_model"] != model:
        return "stale"
    return "ready"


def _check_collection_name(name) -> str:
    if (not isinstance(name, str) or not 1 <= len(name) <= 60 or name.strip() != name
            or any(ord(c) < 32 for c in name) or "/" in name or "\\" in name):
        raise ValueError("컬렉션 이름은 앞뒤 공백 없이 1~60자로 정해 주세요. / 와 \\ 는 쓸 수 없어요.")
    return name


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
            db.executescript(_DOCUMENTS_TABLE.format(table="IF NOT EXISTS documents") + ''';
                CREATE TABLE IF NOT EXISTS chunks (
                    document_id TEXT NOT NULL, page INTEGER NOT NULL,
                    start INTEGER NOT NULL, end INTEGER NOT NULL, text TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS chunks_document ON chunks(document_id);
                CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL);
                INSERT OR IGNORE INTO state VALUES(1,0);
                CREATE TABLE IF NOT EXISTS answers (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS collections (
                    id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL);
                INSERT OR IGNORE INTO collections VALUES('default','기본','1970-01-01T00:00:00+00:00');
                CREATE TABLE IF NOT EXISTS vectors (
                    document_id TEXT NOT NULL, page INTEGER NOT NULL, start INTEGER NOT NULL,
                    vector BLOB NOT NULL, PRIMARY KEY(document_id, page, start));
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            self._migrate(db)
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _migrate(db):
        def columns():
            return {row[1] for row in db.execute("PRAGMA table_info(documents)")}
        if _DOCUMENT_COLUMNS <= columns():
            return
        db.execute("BEGIN IMMEDIATE")
        try:
            present = columns()
            # Stores created before index metadata existed were built by the
            # first lexical retriever and chunker, so they default to those.
            if "retrieval" not in present:
                db.execute("ALTER TABLE documents ADD COLUMN retrieval TEXT NOT NULL DEFAULT 'local_lexical_v1'")
            if "chunking" not in present:
                db.execute("ALTER TABLE documents ADD COLUMN chunking TEXT NOT NULL DEFAULT 'chunk900_overlap100_v1'")
            for column, kind in (("embedding_model", "TEXT"), ("embedding_dim", "INTEGER")):
                if column not in present:
                    db.execute(f"ALTER TABLE documents ADD COLUMN {column} {kind}")
            if "collection_id" not in present:
                # Names were unique across the whole store; now they are unique
                # per collection. SQLite cannot drop a UNIQUE column constraint,
                # so the table is rebuilt and every document joins the default
                # collection within the same transaction.
                db.execute("DROP TABLE IF EXISTS documents_migrating")
                db.execute(_DOCUMENTS_TABLE.format(table="documents_migrating"))
                db.execute("INSERT INTO documents_migrating(id,collection_id,name,sha256,size_bytes,pages,imported_at,retrieval,chunking,embedding_model,embedding_dim) "
                           "SELECT id,'default',name,sha256,size_bytes,pages,imported_at,retrieval,chunking,embedding_model,embedding_dim FROM documents")
                db.execute("DROP TABLE documents")
                db.execute("ALTER TABLE documents_migrating RENAME TO documents")
            db.commit()
        except BaseException:
            db.rollback()
            raise

    # -- settings ---------------------------------------------------------

    @staticmethod
    def _settings(db) -> dict:
        values = {row["key"]: row["value"] for row in db.execute("SELECT key, value FROM meta")}
        return {"model": values.get("embedding_model", document_embeddings.DEFAULT_MODEL),
                "base_url": values.get("embedding_url", document_embeddings.DEFAULT_URL),
                "keep_alive": values.get("embedding_keep_alive", document_embeddings.DEFAULT_KEEP_ALIVE)}

    def embedding_settings(self) -> dict:
        with self.connection() as db:
            return self._settings(db)

    def set_embedding_model(self, model: str, base_url: str | None = None, keep_alive: str | None = None) -> dict:
        if not isinstance(model, str) or not _MODEL_NAME.fullmatch(model):
            raise ValueError("임베딩 모델 이름을 확인해 주세요. 예: bge-m3")
        if keep_alive is not None:
            keep_alive = str(document_embeddings.keep_alive_value(keep_alive))
        if base_url is not None:
            from omm.engines.base import require_loopback_base_url
            try:
                base_url = require_loopback_base_url(base_url)
            except ValueError:
                raise ValueError("임베딩 서버 주소는 이 컴퓨터의 Ollama 주소(예: http://127.0.0.1:11434)만 쓸 수 있어요.") from None
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR REPLACE INTO meta VALUES('embedding_model', ?)", (model,))
            if base_url is not None:
                db.execute("INSERT OR REPLACE INTO meta VALUES('embedding_url', ?)", (base_url,))
            if keep_alive is not None:
                db.execute("INSERT OR REPLACE INTO meta VALUES('embedding_keep_alive', ?)", (keep_alive,))
            settings = self._settings(db)
        return settings

    def _embed(self, texts, settings, progress=None):
        """(vectors, None) or (None, reason message); never raises for an unavailable server.

        ``progress(done, total)`` runs after each embedding batch.
        """
        if not texts:
            return [], None
        try:
            return _embedder(settings["model"], settings["base_url"]).embed(
                texts, keep_alive=_keep_alive(settings), progress=progress), None
        except document_embeddings.EmbeddingUnavailable as error:
            return None, str(error)

    # -- collections -------------------------------------------------------

    @staticmethod
    def _collection(db, name):
        if name is None:
            return db.execute("SELECT * FROM collections WHERE id=?", (DEFAULT_COLLECTION,)).fetchone()
        row = db.execute("SELECT * FROM collections WHERE name=?", (name,)).fetchone()
        if row is None:
            raise ValueError(f"'{name}' 컬렉션을 찾지 못했어요. `omm rag collection list`로 이름을 확인해 주세요.")
        return row

    def collections(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT c.id, c.name, c.created_at, (SELECT COUNT(*) FROM documents WHERE collection_id=c.id) AS documents "
                "FROM collections c ORDER BY c.id != 'default', c.name")]

    def create_collection(self, name):
        name = _check_collection_name(name)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM collections WHERE name=?", (name,)).fetchone():
                raise ValueError(f"'{name}' 컬렉션이 이미 있어요.")
            key = str(uuid.uuid4())
            db.execute("INSERT INTO collections VALUES(?,?,?)", (key, name, datetime.now(timezone.utc).isoformat()))
        return {"id": key, "name": name}

    def delete_collection(self, name):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._collection(db, name)
            if row["id"] == DEFAULT_COLLECTION:
                raise ValueError("기본 컬렉션은 지울 수 없어요. 안의 문서는 하나씩 지울 수 있어요.")
            ids = [r[0] for r in db.execute("SELECT id FROM documents WHERE collection_id=?", (row["id"],))]
            for key in ids:
                self._remove(db, key)
            db.execute("DELETE FROM collections WHERE id=?", (row["id"],))
            self._changed(db)
        return {"removed": True, "name": name, "documents": len(ids)}

    # -- documents ---------------------------------------------------------

    def list(self, collection=None):
        with self.connection() as db:
            model = self._settings(db)["model"]
            where, args = "", ()
            if collection is not None:
                where, args = "WHERE d.collection_id=?", (self._collection(db, collection)["id"],)
            rows = [dict(row) for row in db.execute(
                "SELECT d.*, c.name AS collection, (SELECT COUNT(*) FROM chunks WHERE document_id=d.id) AS chunks "
                f"FROM documents d JOIN collections c ON c.id=d.collection_id {where} ORDER BY c.id != 'default', c.name, d.name", args)]
        for row in rows:
            row["index_current"] = index_current(row)
            row["semantic"] = semantic_state(row, model)
            row["reindex_needed"] = row["semantic"] in {"missing", "stale"}
        return rows

    def status(self, *, check_embedding=False) -> dict:
        documents = self.list()
        settings = self.embedding_settings()
        result = {"embedding": {**settings, "available": None, "notice": None},
                  "collections": self.collections(), "documents": len(documents),
                  "semantic_ready": sum(d["semantic"] == "ready" for d in documents),
                  "reindex_needed": sum(d["reindex_needed"] for d in documents),
                  "reregister_needed": sum(d["semantic"] == "reregister" for d in documents)}
        if check_embedding:
            try:
                available = _embedder(settings["model"], settings["base_url"]).available()
            except document_embeddings.EmbeddingUnavailable as error:
                result["embedding"]["notice"] = str(error)
            else:
                result["embedding"]["available"] = available
                if not available:
                    result["embedding"]["notice"] = f"Ollama에 {settings['model']} 모델이 없어요. " + document_embeddings.pull_hint(settings["model"])
        return result

    @staticmethod
    def _admit(db, collection_id, name, digest, pieces, replace):
        """(existing row or None, unchanged) when this import may proceed."""
        old = db.execute("SELECT * FROM documents WHERE collection_id=? AND name=?", (collection_id, name)).fetchone()
        if old and old["sha256"] == digest and index_current(old):
            return old, True
        if old and not replace:
            raise ValueError("같은 이름의 문서가 있어요. 교체를 선택해 주세요.")
        if not old and db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] >= MAX_DOCUMENTS:
            raise ValueError("문서는 100개까지 등록할 수 있어요.")
        total = db.execute("SELECT COALESCE(SUM(LENGTH(text)),0) FROM chunks WHERE document_id != ?", (old["id"] if old else "",)).fetchone()[0]
        if total + sum(len(row[3]) for row in pieces) > 2_000_000:
            raise ValueError("전체 문서의 검색 텍스트는 200만 자까지 지원해요. 기존 문서를 정리해 주세요.")
        return old, False

    def import_document(self, name: str, content: bytes, *, replace=False, collection=None, progress=None):
        """Store and index one document.

        ``progress(stage, done, total)`` reports ``("extract", 0, 0)`` before
        text extraction and ``("embed", done, total)`` after each embedding
        batch, so a caller can show that a long CPU embedding is moving.
        """
        if not isinstance(name, str) or not 1 <= len(name) <= 180 or Path(name).name != name or any(ord(c) < 32 for c in name) or "\\" in name:
            raise ValueError("문서 파일 이름을 확인해 주세요.")
        if progress is not None:
            progress("extract", 0, 0)
        pages = extract(name, content)
        pieces = list(chunks(pages))
        digest = hashlib.sha256(content).hexdigest()
        with self.connection() as db:
            target = self._collection(db, collection)
            settings = self._settings(db)
            old, unchanged = self._admit(db, target["id"], name, digest, pieces, replace)
        base = {"name": name, "collection": target["name"]}
        if unchanged:
            result = {"id": old["id"], **base, "unchanged": True}
            if semantic_state(old, settings["model"]) != "ready":
                outcome = self._reindex_one(old["id"], settings, progress)
                return {**result, "semantic": outcome is None, "semantic_notice": outcome}
            return {**result, "semantic": True, "semantic_notice": None}
        # Embedding can take a while on CPU; it runs before the write lock so
        # other readers are not blocked, and a failure keeps the lexical index.
        vectors, notice = self._embed([row[3] for row in pieces], settings, _stage(progress, "embed"))
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old, unchanged = self._admit(db, target["id"], name, digest, pieces, replace)
            if unchanged:
                return {"id": old["id"], **base, "unchanged": True,
                        "semantic": semantic_state(old, settings["model"]) == "ready", "semantic_notice": None}
            key = old["id"] if old else str(uuid.uuid4())
            db.execute("DELETE FROM chunks WHERE document_id=?", (key,))
            db.execute("DELETE FROM vectors WHERE document_id=?", (key,))
            dim = len(vectors[0]) if vectors else None
            db.execute("INSERT OR REPLACE INTO documents(id,collection_id,name,sha256,size_bytes,pages,imported_at,retrieval,chunking,embedding_model,embedding_dim) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (key, target["id"], name, digest, len(content), max(p for p, _ in pages), datetime.now(timezone.utc).isoformat(),
                        RETRIEVAL_METHOD, CHUNKING_VERSION, settings["model"] if vectors else None, dim))
            db.executemany("INSERT INTO chunks VALUES(?,?,?,?,?)", [(key, *row) for row in pieces])
            if vectors:
                db.executemany("INSERT OR REPLACE INTO vectors VALUES(?,?,?,?)",
                               [(key, row[0], row[1], document_embeddings.pack(vector)) for row, vector in zip(pieces, vectors)])
            self._changed(db)
        if notice:
            notice = f"단어 검색 색인만 만들었어요. {notice} 그 뒤 `omm rag reindex`로 의미 색인을 만들 수 있어요."
        return {"id": key, **base, "chunks": len(pieces), "sha256": digest, "semantic": bool(vectors), "semantic_notice": notice}

    def _reindex_one(self, key, settings, progress=None):
        """Re-embed one document's stored chunks. None on success, else the reason."""
        with self.connection() as db:
            row = db.execute("SELECT sha256, chunking FROM documents WHERE id=?", (key,)).fetchone()
            if row is None:
                return "문서가 바뀌었어요. 목록을 새로고침해 주세요."
            pieces = [dict(r) for r in db.execute("SELECT page, start, text FROM chunks WHERE document_id=? ORDER BY page, start", (key,))]
        vectors, notice = self._embed([p["text"] for p in pieces], settings, _stage(progress, "embed"))
        if vectors is None:
            return notice
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = db.execute("SELECT sha256, chunking FROM documents WHERE id=?", (key,)).fetchone()
            if now is None or tuple(now) != tuple(row):
                return "색인하는 동안 문서가 바뀌었어요. 다시 실행해 주세요."
            db.execute("DELETE FROM vectors WHERE document_id=?", (key,))
            db.executemany("INSERT OR REPLACE INTO vectors VALUES(?,?,?,?)",
                           [(key, p["page"], p["start"], document_embeddings.pack(v)) for p, v in zip(pieces, vectors)])
            db.execute("UPDATE documents SET embedding_model=?, embedding_dim=? WHERE id=?",
                       (settings["model"] if vectors else None, len(vectors[0]) if vectors else None, key))
        return None

    def reindex(self, collection=None, *, everything=False, progress=None) -> dict:
        """Build semantic vectors for documents without current ones (all with everything=True).

        ``progress(name, stage, done, total)`` reports each embedding batch.
        """
        settings = self.embedding_settings()
        documents = self.list(collection)
        result = {"model": settings["model"], "reindexed": [], "already_current": 0, "failed": None,
                  "reregister": [d["name"] for d in documents if d["semantic"] == "reregister"]}
        for document in documents:
            if document["semantic"] == "reregister":
                continue
            if document["semantic"] == "ready" and not everything:
                result["already_current"] += 1
                continue
            notice = self._reindex_one(document["id"], settings, _named(progress, document["name"]))
            if notice:
                result["failed"] = {"name": document["name"], "notice": notice}
                break
            result["reindexed"].append(document["name"])
        return result

    @staticmethod
    def _changed(db):
        db.execute("UPDATE state SET revision=revision+1 WHERE id=1")
        # A removed source must not survive in cached answer quotations.
        db.execute("DELETE FROM answers")

    @staticmethod
    def _remove(db, key):
        db.execute("DELETE FROM chunks WHERE document_id=?", (key,))
        db.execute("DELETE FROM vectors WHERE document_id=?", (key,))
        db.execute("DELETE FROM documents WHERE id=?", (key,))

    def delete(self, key):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM documents WHERE id=?", (key,)).fetchone():
                raise ValueError("문서를 찾을 수 없어요. 목록을 새로고침해 주세요.")
            self._remove(db, key)
            self._changed(db)
        return {"removed": True, "id": key}

    def remove_document(self, collection, name):
        with self.connection() as db:
            target = self._collection(db, collection)
            row = db.execute("SELECT id FROM documents WHERE collection_id=? AND name=?", (target["id"], name)).fetchone()
        if row is None:
            raise ValueError(f"'{target['name']}' 컬렉션에 '{name}' 문서가 없어요.")
        return {**self.delete(row["id"]), "name": name, "collection": target["name"]}

    # -- search ------------------------------------------------------------

    def search(self, question: str, ids: list[str] | None, *, collection=None, limit=3):
        """(revision, sources). Each source also carries its search ``method``."""
        result = self.search_detailed(question, ids, collection=collection, limit=limit)
        return result["revision"], result["sources"]

    def search_detailed(self, question: str, ids: list[str] | None = None, *, collection=None, limit=3) -> dict:
        """Hybrid (semantic + lexical) search, falling back to lexical with a reason.

        ``ids=None`` searches every document (of ``collection`` when given).
        Returns revision, sources, method (``hybrid`` or ``lexical``) and an
        optional plain-language notice explaining a fallback.
        """
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("검색 결과 수는 1~10개로 정해 주세요.")
        query = set(terms(question))
        with self.connection() as db:
            revision = db.execute("SELECT revision FROM state WHERE id=1").fetchone()[0]
            settings = self._settings(db)
            scope = self._collection(db, collection) if collection is not None else None
            known = {row["id"]: row for row in db.execute(
                "SELECT d.id, d.name, d.collection_id, d.retrieval, d.chunking, d.embedding_model, d.embedding_dim FROM documents d")}
            if ids is None:
                ids = sorted(key for key, row in known.items() if scope is None or row["collection_id"] == scope["id"])
                if not ids:
                    return {"revision": revision, "sources": [], "method": LEXICAL, "embedding_model": None,
                            "notice": "검색할 문서가 없어요. 먼저 문서를 등록해 주세요."}
            else:
                if not ids or len(ids) > 20 or len(set(ids)) != len(ids):
                    raise ValueError("질문할 문서를 1~20개 선택해 주세요.")
                if not set(ids) <= set(known):
                    raise ValueError("선택한 문서가 바뀌었어요. 문서 목록을 새로고침해 주세요.")
                if scope is not None and any(known[key]["collection_id"] != scope["id"] for key in ids):
                    raise ValueError(f"선택한 문서 중 '{scope['name']}' 컬렉션에 없는 문서가 있어요.")
            stale = sorted(known[key]["name"] for key in ids if not index_current(known[key]))
            if stale:
                raise ValueError(f"검색 색인 방식이 바뀌어 다시 등록해야 하는 문서가 있어요: {', '.join(stale)}. "
                                 "원본 파일을 '같은 이름의 문서 교체'로 다시 등록하면 새 방식으로 색인돼요.")
            placeholders = ",".join("?" for _ in ids)
            rows = [dict(r) for r in db.execute(
                f"SELECT c.*, d.name, d.sha256, col.name AS collection FROM chunks c JOIN documents d ON d.id=c.document_id "
                f"JOIN collections col ON col.id=d.collection_id WHERE d.id IN ({placeholders})", ids)]
            ready = [key for key in ids if semantic_state(known[key], settings["model"]) == "ready"]
            vectors = {}
            if ready:
                marks = ",".join("?" for _ in ready)
                vectors = {(r["document_id"], r["page"], r["start"]): document_embeddings.unpack(r["vector"])
                           for r in db.execute(f"SELECT * FROM vectors WHERE document_id IN ({marks})", ready)}
        lexical = _lexical_ranking(query, rows) if query else []
        method, notice, semantic = LEXICAL, None, []
        if not ready:
            notice = "의미 검색 색인이 없어서 단어 검색만 했어요. `omm rag reindex`로 의미 색인을 만들 수 있어요." if rows else None
        else:
            try:
                question_vector = _embedder(settings["model"], settings["base_url"]).embed(
                    [question], keep_alive=_keep_alive(settings))[0]
            except document_embeddings.EmbeddingUnavailable as error:
                notice = f"{error} 그래서 단어 검색만 했어요."
            else:
                method = HYBRID
                semantic = _semantic_ranking(question_vector, rows, vectors)
                if len(ready) < len(ids):
                    notice = (f"{len(ids) - len(ready)}개 문서는 의미 색인이 없거나 임베딩 모델이 바뀌어 단어 검색만 했어요. "
                              "`omm rag reindex`로 다시 색인해 주세요.")
        ordered = _fuse(lexical, semantic) if method == HYBRID else [(score, row, "lexical", None) for score, row in lexical]
        sources = []
        for score, row, match, similarity in ordered:
            if any(s["document_id"] == row["document_id"] and s["page"] == row["page"] and abs(s["start"] - row["start"]) < 450 for s in sources):
                continue
            sources.append({**row, "citation": len(sources) + 1, "method": method, "match": match,
                            "score": round(score, 6), "similarity": None if similarity is None else round(similarity, 4)})
            if len(sources) == limit:
                break
        return {"revision": revision, "sources": sources, "method": method, "notice": notice,
                "embedding_model": settings["model"] if method == HYBRID else None}

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


def configured_embedding_model() -> str:
    """The document-search embedding model (without ``:latest``); never creates the store."""
    if not (config.OMM_HOME / "documents" / "library.sqlite3").is_file():
        return document_embeddings.DEFAULT_MODEL
    return DocumentStore().embedding_settings()["model"].removesuffix(":latest")


def without_embedding_model(models, engine: str) -> list:
    """Runtime models minus the warm document-search embedding model.

    Searches keep that model loaded for a few minutes (``keep_alive``). It is
    not another chat or answer task, so checks for "another model is busy"
    skip it; memory checks still see the RAM it uses.
    """
    models = list(models)
    if engine != "ollama":
        return models
    embedding = configured_embedding_model()
    return [m for m in models if str(getattr(m, "key", "")).removesuffix(":latest") != embedding]


def _keep_alive(settings):
    try:
        return document_embeddings.keep_alive_value(settings.get("keep_alive", document_embeddings.DEFAULT_KEEP_ALIVE))
    except ValueError:
        return document_embeddings.DEFAULT_KEEP_ALIVE


def _stage(progress, stage):
    """Adapt ``progress(stage, done, total)`` to the embedder's ``(done, total)``."""
    return None if progress is None else (lambda done, total: progress(stage, done, total))


def _named(progress, name):
    """Adapt ``progress(name, stage, done, total)`` to one document's ``(stage, done, total)``."""
    return None if progress is None else (lambda stage, done, total: progress(name, stage, done, total))


def _order(row):
    return row["name"], row["page"], row["start"]


def _lexical_ranking(query, rows):
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
    scored.sort(key=lambda x: (-x[0], *_order(x[1])))
    return scored


def _semantic_ranking(question_vector, rows, vectors):
    scored = []
    for row in rows:
        vector = vectors.get((row["document_id"], row["page"], row["start"]))
        if vector is not None and len(vector) == len(question_vector):
            scored.append((document_embeddings.dot(question_vector, vector), row))
    if not scored:
        return []
    cutoff = max(SEMANTIC_MIN_SIMILARITY, max(score for score, _ in scored) - SEMANTIC_MARGIN)
    kept = [item for item in scored if item[0] >= cutoff]
    kept.sort(key=lambda x: (-x[0], *_order(x[1])))
    return kept


def _fuse(lexical, semantic):
    """Reciprocal rank fusion: rank positions only, so the two score scales never mix."""
    fused = {}
    for kind, ranking in (("lexical", lexical), ("semantic", semantic)):
        for rank, (score, row) in enumerate(ranking, 1):
            key = (row["document_id"], row["page"], row["start"])
            entry = fused.setdefault(key, {"row": row, "score": 0.0, "kinds": set(), "similarity": None})
            entry["score"] += 1 / (RRF_K + rank)
            entry["kinds"].add(kind)
            if kind == "semantic":
                entry["similarity"] = score
    ordered = sorted(fused.values(), key=lambda e: (-e["score"], *_order(e["row"])))
    return [(e["score"], e["row"], "both" if len(e["kinds"]) == 2 else next(iter(e["kinds"])), e["similarity"]) for e in ordered]


def import_request(request):
    try:
        content = base64.b64decode(request["content"], validate=True)
    except (ValueError, TypeError):
        raise ValueError("파일 전송 내용을 확인하지 못했어요.") from None
    return DocumentStore().import_document(request["filename"], content, replace=request.get("replace") is True)


def import_paths(collection, paths, *, replace=False, progress=None):
    """Import files and directories (recursively) from disk into a collection.

    Unsupported suffixes inside a directory are skipped and counted; an
    explicitly named unsupported file is reported as a failure.
    ``progress(name, stage, done, total)`` reports each file's steps.
    """
    store = DocumentStore()
    with store.connection() as db:
        store._collection(db, collection)
    files, skipped = collect_files(paths)
    results = []
    for path, problem in files:
        if problem is None:
            try:
                if path.stat().st_size > MAX_UPLOAD:
                    raise ValueError("비어 있거나 6 MiB를 넘는 문서는 등록할 수 없어요.")
                results.append({"path": str(path), **store.import_document(path.name, path.read_bytes(), replace=replace, collection=collection,
                                                                          progress=_named(progress, path.name))})
                continue
            except (ValueError, OSError) as error:
                problem = str(error) if isinstance(error, ValueError) else "파일을 읽지 못했어요. 권한을 확인해 주세요."
        results.append({"path": str(path), "name": path.name, "error": problem})
    return {"collection": collection, "results": results, "skipped": skipped}


def collect_files(paths):
    """([(path, problem or None)], skipped count) for files and directories (recursive).

    Shared by local import and upload to a remote cluster head.
    """
    files, skipped = [], 0
    for raw in paths:
        path = Path(raw)
        if path.is_symlink():
            files.append((path, "연결 경로(symlink)는 등록하지 않아요."))
        elif path.is_dir():
            for item in sorted(path.rglob("*")):
                if item.is_symlink() or not item.is_file():
                    continue
                if item.suffix.lower() in SUPPORTED_SUFFIXES:
                    files.append((item, None))
                else:
                    skipped += 1
        elif path.is_file():
            files.append((path, None))
        else:
            files.append((path, "파일이나 폴더를 찾지 못했어요."))
    return files, skipped
