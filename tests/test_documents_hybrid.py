"""Hybrid (semantic + lexical) document search against a fake loopback Ollama."""
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from typer.testing import CliRunner

from omm import document_embeddings, documents
from omm.documents import DocumentStore

# Deterministic "meaning" axes: paraphrases share an axis without sharing words.
CONCEPTS = [("연차", "휴가", "쉬는", "휴일"), ("출장", "숙박", "호텔"), ("서버", "점검")]

LEAVE = '연차 휴가는 입사 1년 후 15일이 주어집니다.'.encode()
TRAVEL = '출장 숙박비는 1박 10만원 한도입니다.'.encode()
# Long enough for more than one embedding batch.
LONG = "\n\n".join(f'연차 휴가 조항 {i}. ' + '가나다라마바사 ' * 120 for i in range(40))


def fake_vector(text, dims):
    vector = [float(sum(text.count(word) for word in words)) for words in CONCEPTS]
    vector += [0.0] * (dims - len(vector))
    if not any(vector):
        vector[-1] = 1.0  # unrelated text points somewhere else entirely
    return vector


class FakeOllama:
    def __init__(self):
        self.models = {"bge-m3": 8, "other-embed": 6}
        self.requests = []
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/tags":
                    return self._send(200, {"models": [{"name": f"{name}:latest", "model": f"{name}:latest"} for name in fake.models]})
                self._send(404, {"error": "not found"})

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(payload)
                dims = fake.models.get(payload["model"])
                if self.path != "/api/embed" or dims is None:
                    return self._send(404, {"error": f'model "{payload["model"]}" not found, try pulling it first'})
                self._send(200, {"model": payload["model"], "embeddings": [fake_vector(text, dims) for text in payload["input"]]})

        return Handler

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def ollama(isolated_omm_home, monkeypatch):
    fake = FakeOllama()
    monkeypatch.setattr(documents, "_embedder", document_embeddings.OllamaEmbedder)
    DocumentStore().set_embedding_model("bge-m3", base_url=fake.url)
    yield fake
    fake.close()


@pytest.fixture
def offline(isolated_omm_home, monkeypatch):
    """Real client pointed at a closed loopback port: Ollama is off."""
    import socket
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setattr(documents, "_embedder", document_embeddings.OllamaEmbedder)
    DocumentStore().set_embedding_model("bge-m3", base_url=f"http://127.0.0.1:{port}")


def _vector_count(home, key=None):
    db = sqlite3.connect(home / "documents/library.sqlite3")
    try:
        if key is None:
            return db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
        return db.execute("SELECT COUNT(*) FROM vectors WHERE document_id=?", (key,)).fetchone()[0]
    finally:
        db.close()


def test_paraphrase_found_by_hybrid_but_not_by_words(ollama):
    store = DocumentStore()
    leave = store.import_document('휴가규정.txt', LEAVE)
    travel = store.import_document('출장규정.txt', TRAVEL)
    assert leave['semantic'] and travel['semantic'] and leave['semantic_notice'] is None
    question = '쉬는 날 며칠 받을 수 있어?'
    lexical = documents._lexical_ranking(set(documents.terms(question)), [{'text': LEAVE.decode()}, {'text': TRAVEL.decode()}])
    assert lexical == []
    result = store.search_detailed(question, [leave['id'], travel['id']])
    assert result['method'] == 'hybrid' and result['notice'] is None and result['embedding_model'] == 'bge-m3'
    assert [s['name'] for s in result['sources']] == ['휴가규정.txt']
    source = result['sources'][0]
    assert source['match'] == 'semantic' and source['method'] == 'hybrid' and source['similarity'] > 0.9
    assert source['text'] == LEAVE.decode()[source['start']:source['end']]
    # The model stays warm for the default window so the next question is fast.
    assert ollama.requests[-1] == {'model': 'bge-m3', 'input': [question], 'truncate': True, 'keep_alive': '5m'}


def test_fusion_ranks_chunks_found_by_both_methods_first(ollama):
    store = DocumentStore()
    words = store.import_document('a.txt', '신청 양식은 인사팀 메일로 보냅니다.'.encode())
    both = store.import_document('b.txt', '연차 휴가 신청 양식은 결재 시스템에 있습니다.'.encode())
    meaning = store.import_document('c.txt', '쉬는 날 규정'.encode())
    _, sources = store.search('휴가 신청 양식', [words['id'], both['id'], meaning['id']])
    assert [(s['name'], s['match']) for s in sources] == [('b.txt', 'both'), ('a.txt', 'lexical'), ('c.txt', 'semantic')]
    assert sources[0]['score'] > sources[1]['score'] == sources[2]['score']
    assert [s['citation'] for s in sources] == [1, 2, 3]


def test_ollama_off_falls_back_to_lexical_and_says_so(offline):
    store = DocumentStore()
    saved = store.import_document('휴가규정.txt', LEAVE)
    assert saved['semantic'] is False and 'omm rag reindex' in saved['semantic_notice'] and 'Ollama' in saved['semantic_notice']
    assert store.list()[0]['semantic'] == 'missing' and store.list()[0]['reindex_needed']
    result = store.search_detailed('연차 휴가 며칠', [saved['id']])
    assert result['method'] == 'lexical' and 'reindex' in result['notice']
    assert result['sources'][0]['method'] == 'lexical'
    assert store.search_detailed('쉬는 날 며칠 받을 수 있어?', [saved['id']])['sources'] == []


def test_server_lost_after_indexing_reports_lexical_fallback(ollama, monkeypatch):
    store = DocumentStore()
    saved = store.import_document('휴가규정.txt', LEAVE)
    ollama.close()
    result = store.search_detailed('연차 휴가 며칠', [saved['id']])
    assert result['method'] == 'lexical' and 'Ollama' in result['notice'] and '단어 검색만' in result['notice']
    assert result['sources'] and result['sources'][0]['match'] == 'lexical'


def test_missing_embedding_model_hints_ollama_pull(ollama):
    store = DocumentStore()
    store.set_embedding_model('not-pulled')
    saved = store.import_document('휴가규정.txt', LEAVE)
    assert 'ollama pull not-pulled' in saved['semantic_notice']
    status = store.status(check_embedding=True)
    assert status['embedding']['available'] is False and 'ollama pull not-pulled' in status['embedding']['notice']


def test_embedding_model_change_requires_reindex_then_matches_storage(ollama, isolated_omm_home):
    store = DocumentStore()
    saved = store.import_document('휴가규정.txt', LEAVE)
    other = store.import_document('출장규정.txt', TRAVEL)
    assert store.status()['reindex_needed'] == 0
    store.set_embedding_model('other-embed')
    rows = store.list()
    assert {r['semantic'] for r in rows} == {'stale'} and all(r['reindex_needed'] for r in rows)
    assert store.status()['reindex_needed'] == 2
    # Stale vectors are never compared with the new model's question vector.
    result = store.search_detailed('쉬는 날 며칠 받을 수 있어?', [saved['id'], other['id']])
    assert result['method'] == 'lexical' and result['sources'] == [] and 'reindex' in result['notice']
    report = store.reindex()
    assert sorted(report['reindexed']) == ['출장규정.txt', '휴가규정.txt'] and report['failed'] is None
    rows = store.list()
    assert all(r['semantic'] == 'ready' and r['embedding_model'] == 'other-embed' and r['embedding_dim'] == 6 for r in rows)
    assert _vector_count(isolated_omm_home) == sum(r['chunks'] for r in rows)
    assert store.search_detailed('쉬는 날 며칠 받을 수 있어?', [saved['id'], other['id']])['sources'][0]['name'] == '휴가규정.txt'
    assert store.reindex()['already_current'] == 2


def test_partial_semantic_index_is_reported(ollama):
    store = DocumentStore()
    ready = store.import_document('휴가규정.txt', LEAVE)
    ollama.models.pop('bge-m3')
    missing = store.import_document('출장규정.txt', TRAVEL)
    ollama.models['bge-m3'] = 8
    result = store.search_detailed('쉬는 날 며칠 받을 수 있어?', [ready['id'], missing['id']])
    assert result['method'] == 'hybrid' and result['notice'].startswith('1개 문서')
    assert [s['name'] for s in result['sources']] == ['휴가규정.txt']


def test_unrelated_question_has_no_evidence_and_no_model_contact(ollama, monkeypatch):
    from omm.web import document_questions as q
    saved = DocumentStore().import_document('휴가규정.txt', LEAVE)
    monkeypatch.setattr(q, 'adapter_for', lambda *a: pytest.fail('no model contact without evidence'))
    result = q.answer({'question': '공룡은 왜 멸종했어?', 'document_ids': [saved['id']], 'filename': 'none.gguf', 'engine': 'ollama'}, threading.Event())
    assert result['status'] == 'no_evidence' and result['sources'] == []
    assert result['retrieval'] == 'hybrid' and result['retrieval_notice'] is None


def test_replace_and_delete_update_vectors_and_cached_answers(ollama, isolated_omm_home):
    store = DocumentStore()
    saved = store.import_document('규정.txt', LEAVE)
    revision, sources = store.search('쉬는 날 며칠', [saved['id']])
    store.save_answer({'id': 'a', 'created_at': '2026-10-24', 'sources': sources}, revision)
    replaced = store.import_document('규정.txt', '서버 점검은 화요일 새벽입니다.'.encode(), replace=True)
    assert replaced['id'] == saved['id'] and store.answers() == []
    assert _vector_count(isolated_omm_home, saved['id']) == store.list()[0]['chunks'] == 1
    assert store.search('쉬는 날 며칠', [saved['id']])[1] == []
    assert store.search('서버 정기 작업', [saved['id']])[1][0]['text'] == '서버 점검은 화요일 새벽입니다.'
    store.delete(saved['id'])
    assert _vector_count(isolated_omm_home) == 0 and store.list() == []


def test_same_bytes_reimport_builds_missing_semantic_index(ollama):
    store = DocumentStore()
    ollama.models.pop('bge-m3')
    saved = store.import_document('휴가규정.txt', LEAVE)
    assert saved['semantic'] is False
    ollama.models['bge-m3'] = 8
    again = store.import_document('휴가규정.txt', LEAVE)
    assert again['unchanged'] and again['semantic'] and store.list()[0]['semantic'] == 'ready'


def test_collections_scope_search_and_names(ollama):
    store = DocumentStore()
    store.create_collection('인사규정')
    store.create_collection('영업자료')
    with pytest.raises(ValueError, match='이미'):
        store.create_collection('인사규정')
    hr = store.import_document('안내.txt', LEAVE, collection='인사규정')
    sales = store.import_document('안내.txt', TRAVEL, collection='영업자료')
    assert hr['id'] != sales['id'] and hr['collection'] == '인사규정'
    result = store.search_detailed('쉬는 날 며칠 받을 수 있어?', None, collection='영업자료')
    assert result['sources'] == []
    result = store.search_detailed('쉬는 날 며칠 받을 수 있어?', None, collection='인사규정')
    assert result['sources'][0]['collection'] == '인사규정'
    with pytest.raises(ValueError, match='컬렉션에 없는'):
        store.search('휴가', [sales['id']], collection='인사규정')
    with pytest.raises(ValueError, match='찾지 못했어요'):
        store.search('휴가', None, collection='없는컬렉션')
    with pytest.raises(ValueError, match='기본 컬렉션'):
        store.delete_collection('기본')
    assert store.delete_collection('영업자료')['documents'] == 1
    assert [d['name'] for d in store.list()] == ['안내.txt'] and store.list()[0]['collection'] == '인사규정'


def test_old_store_migrates_into_default_collection(isolated_omm_home):
    root = isolated_omm_home / 'documents'
    root.mkdir(parents=True)
    db = sqlite3.connect(root / 'library.sqlite3')
    db.executescript("""CREATE TABLE documents (id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, sha256 TEXT NOT NULL,
        size_bytes INTEGER NOT NULL, pages INTEGER NOT NULL, imported_at TEXT NOT NULL,
        retrieval TEXT NOT NULL DEFAULT 'local_lexical_v1', chunking TEXT NOT NULL DEFAULT 'chunk900_overlap100_v1');
        CREATE TABLE chunks (document_id TEXT NOT NULL, page INTEGER NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL, text TEXT NOT NULL);
        CREATE TABLE state (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL);
        INSERT INTO state VALUES(1,7);
        CREATE TABLE answers (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);
        INSERT INTO answers VALUES('kept',7,'2026-10-01','{"id":"kept"}');
        INSERT INTO documents VALUES('old','old.txt','x',10,1,'2026-10-01','local_lexical_v1','chunk900_overlap100_v1');
        INSERT INTO chunks VALUES('old',1,0,24,'zebra migration notes ok');""")
    db.commit()
    db.close()
    store = DocumentStore()
    row = store.list()[0]
    assert row['collection'] == '기본' and row['collection_id'] == 'default'
    assert row['index_current'] and row['semantic'] == 'missing'
    assert store.answers() == [{'id': 'kept'}]
    assert store.search('zebra migration', ['old'])[1][0]['text'] == 'zebra migration notes ok'
    # Names are now unique per collection, not across the store.
    store.create_collection('다른팀')
    store.import_document('old.txt', b'zebra elsewhere notes', collection='다른팀')
    with pytest.raises(ValueError, match='교체'):
        store.import_document('old.txt', b'zebra again notes')
    assert len(DocumentStore().list()) == 2


def test_import_paths_reads_directory_recursively(ollama, tmp_path):
    folder = tmp_path / '자료'
    (folder / 'sub').mkdir(parents=True)
    (folder / '휴가.txt').write_bytes(LEAVE)
    (folder / 'sub' / '출장.md').write_bytes(TRAVEL)
    (folder / 'image.png').write_bytes(b'png')
    (folder / 'broken.txt').write_bytes(b'\xff')
    DocumentStore().create_collection('인사규정')
    report = documents.import_paths('인사규정', [folder, tmp_path / 'missing.txt'])
    ok = sorted(r['name'] for r in report['results'] if 'error' not in r)
    failed = sorted(r['name'] for r in report['results'] if 'error' in r)
    assert ok == ['출장.md', '휴가.txt'] and failed == ['broken.txt', 'missing.txt'] and report['skipped'] == 1


def test_rejects_non_loopback_embedding_server(isolated_omm_home):
    with pytest.raises(ValueError, match='이 컴퓨터'):
        DocumentStore().set_embedding_model('bge-m3', base_url='http://192.168.0.10:11434')
    with pytest.raises(ValueError, match='모델 이름'):
        DocumentStore().set_embedding_model('bad model')


def test_vectors_round_trip_as_little_endian_float32():
    blob = document_embeddings.pack([0.6, 0.8])
    assert len(blob) == 8 and blob[:4] == bytes.fromhex('9a99193f')
    assert list(document_embeddings.unpack(blob)) == pytest.approx([0.6, 0.8])


def _cli(*args):
    from omm.cli import app
    return CliRunner().invoke(app, ['rag', *args])


def test_cli_collection_add_search_status_reindex(ollama, tmp_path):
    (tmp_path / '휴가.txt').write_bytes(LEAVE)
    created = _cli('collection', 'create', '인사규정')
    assert created.exit_code == 0, created.output
    added = _cli('add', '인사규정', str(tmp_path / '휴가.txt'))
    assert added.exit_code == 0 and '휴가.txt' in added.output
    listed = _cli('collection', 'list', '--json')
    assert {c['name']: c['documents'] for c in json.loads(listed.output)['collections']} == {'기본': 0, '인사규정': 1}
    found = _cli('search', '쉬는 날 며칠 받을 수 있어?', '--collection', '인사규정', '--json')
    assert found.exit_code == 0, found.output
    value = json.loads(found.output)
    assert value['method'] == 'hybrid' and value['sources'][0]['name'] == '휴가.txt'
    text = _cli('search', '쉬는 날 며칠 받을 수 있어?')
    assert '의미+단어' in text.output and '[1] 휴가.txt' in text.output
    status = _cli('status', '--json')
    assert json.loads(status.output)['embedding']['available'] is True
    changed = _cli('embedding-model', 'set', 'other-embed')
    assert changed.exit_code == 0 and 'omm rag reindex' in changed.output
    assert '임베딩 모델 바뀜 (재색인 필요)' in _cli('status').output
    assert _cli('reindex').exit_code == 0
    assert '재색인 필요 0개' in _cli('status').output
    removed = _cli('remove', '인사규정', '휴가.txt', '--yes')
    assert removed.exit_code == 0 and DocumentStore().list() == []
    assert _cli('collection', 'delete', '인사규정', '--yes').exit_code == 0


def test_cli_embedding_model_set_hints_pull_when_missing(ollama):
    result = _cli('embedding-model', 'set', 'not-pulled')
    assert result.exit_code == 0 and 'ollama pull not-pulled' in result.output
    shown = _cli('embedding-model')
    assert 'not-pulled' in shown.output


def test_rag_crash_is_never_queued_as_crash_report(monkeypatch):
    from omm import cli
    monkeypatch.setattr(cli.sys, 'argv', ['omm', 'rag', 'search', 'PRIVATE_QUESTION'])
    monkeypatch.setattr(cli.error_report, 'queue_report', lambda *a, **k: pytest.fail('rag crash must not be queued'))
    cli._queue_crash_report(RuntimeError('PRIVATE_DOCUMENT_TEXT'))


def test_keep_alive_is_configurable_and_zero_unloads_after_last_batch(ollama):
    store = DocumentStore()
    store.set_embedding_model('bge-m3', keep_alive='0')
    assert store.embedding_settings()['keep_alive'] == '0'
    text = LONG
    saved = store.import_document('긴규정.txt', text.encode())
    embeds = [r for r in ollama.requests if 'input' in r]
    assert len(embeds) >= 2 and saved['chunks'] > document_embeddings.BATCH
    # Between the batches of one request the model stays; it unloads after the last.
    assert [r['keep_alive'] for r in embeds[:-1]] == ['1m'] * (len(embeds) - 1) and embeds[-1]['keep_alive'] == 0
    store.search_detailed('쉬는 날 며칠?', [saved['id']])
    assert ollama.requests[-1]['keep_alive'] == 0
    store.set_embedding_model('bge-m3', keep_alive='1h')
    store.search_detailed('쉬는 날 며칠?', [saved['id']])
    assert ollama.requests[-1]['keep_alive'] == '1h'
    # Changing only the model keeps the chosen window.
    store.set_embedding_model('bge-m3')
    assert store.embedding_settings()['keep_alive'] == '1h'
    for bad in ('5', '-1', '5 minutes', '99999m', ''):
        with pytest.raises(ValueError, match='유지 시간'):
            store.set_embedding_model('bge-m3', keep_alive=bad)


def test_import_and_reindex_report_progress_per_batch(ollama, tmp_path):
    text = LONG
    (tmp_path / '긴규정.txt').write_text(text, encoding='utf-8')
    events = []
    report = documents.import_paths('기본', [tmp_path], progress=lambda *event: events.append(event))
    chunks = report['results'][0]['chunks']
    batches = -(-chunks // document_embeddings.BATCH)
    assert events[0] == ('긴규정.txt', 'extract', 0, 0)
    embed = [e for e in events if e[1] == 'embed']
    # One 0/N line before the first (slow) batch, then one per batch.
    assert batches >= 2 and len(embed) == batches + 1 and batches == len([r for r in ollama.requests if 'input' in r])
    assert [e[2] for e in embed] == [0] + [min(chunks, (i + 1) * document_embeddings.BATCH) for i in range(batches)]
    assert all(e[0] == '긴규정.txt' and e[3] == chunks for e in embed)
    events.clear()
    result = DocumentStore().reindex(everything=True, progress=lambda *event: events.append(event))
    assert result['reindexed'] == ['긴규정.txt'] and len(events) == batches + 1 and events[-1] == ('긴규정.txt', 'embed', chunks, chunks)


def test_cli_add_and_reindex_json_has_no_progress_output(ollama, tmp_path):
    (tmp_path / '휴가.txt').write_bytes(LEAVE)
    added = _cli('add', '기본', str(tmp_path / '휴가.txt'), '--json')
    assert added.exit_code == 0, added.output
    value = json.loads(added.output)  # nothing but the JSON document
    assert value['results'][0]['name'] == '휴가.txt' and value['results'][0]['semantic'] is True
    reindexed = _cli('reindex', '--all', '--json')
    assert reindexed.exit_code == 0 and json.loads(reindexed.output)['reindexed'] == ['휴가.txt']
    # Off a terminal the live line is never drawn, so plain output stays clean too.
    plain = _cli('reindex', '--all')
    assert '의미 색인 만드는 중' not in plain.output and '재색인: 휴가.txt' in plain.output


def test_cli_progress_line_names_document_and_batches(ollama, tmp_path, monkeypatch):
    from omm import cli
    seen = []

    class Recorder:
        def __init__(self, *args, **kwargs):
            seen.append(('disable', kwargs.get('disable')))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def add_task(self, *args, **kwargs):
            return 1

        def update(self, task_id, description=None, **kwargs):
            seen.append(description)

    monkeypatch.setattr(cli, 'Progress', Recorder)
    (tmp_path / '휴가.txt').write_bytes(LEAVE)
    assert _cli('add', '기본', str(tmp_path / '휴가.txt')).exit_code == 0
    assert seen == [('disable', False), '휴가.txt: 텍스트 추출 중',
                    '휴가.txt: 의미 색인 만드는 중 0/1 조각', '휴가.txt: 의미 색인 만드는 중 1/1 조각']
    seen.clear()
    assert _cli('reindex', '--all', '--quiet').exit_code == 0
    assert seen[0] == ('disable', True)


def test_cli_embedding_model_keep_alive_option(ollama):
    result = _cli('embedding-model', 'set', 'bge-m3', '--keep-alive', '0')
    assert result.exit_code == 0 and '바로 내림' in result.output
    assert DocumentStore().embedding_settings()['keep_alive'] == '0'
    assert json.loads(_cli('status', '--json').output)['embedding']['keep_alive'] == '0'
    bad = _cli('embedding-model', 'set', 'bge-m3', '--keep-alive', 'forever')
    assert bad.exit_code == 1 and DocumentStore().embedding_settings()['keep_alive'] == '0'
