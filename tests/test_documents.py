import io
import json
import threading
from dataclasses import dataclass

import pytest
from pypdf import PdfWriter

from omm import config
from omm.documents import DocumentStore, extract
from omm.web.document_requests import validate


def test_utf8_registration_search_exact_quotes_and_restart(isolated_omm_home):
    original = '실험 안내\n자기장 측정 실험의 준비물은 나침반과 코일입니다.\n안전 수칙을 지켜 주세요.'
    store = DocumentStore()
    saved = store.import_document('전자기학.md', original.encode())
    revision, sources = store.search('자기장 측정 준비물은?', [saved['id']])
    assert len(sources) == 1
    assert sources[0]['text'] == original[sources[0]['start']:sources[0]['end']]
    assert sources[0]['name'] == '전자기학.md'
    assert DocumentStore().list()[0]['sha256'] == saved['sha256']
    assert DocumentStore().search('자기장 준비물', [saved['id']])[1] == sources


def test_no_evidence_returns_no_sources(isolated_omm_home):
    store=DocumentStore(); saved=store.import_document('science.txt', b'The magnetic experiment uses a compass and copper wire.')
    assert store.search('dinosaurs extinction asteroid', [saved['id']])[1] == []


def test_replace_is_atomic_and_old_index_is_removed(isolated_omm_home):
    store=DocumentStore(); saved=store.import_document('notes.txt', b'alpha zebras migrate annually.')
    with pytest.raises(ValueError, match='교체'):
        store.import_document('notes.txt', b'beta dolphins eat fish.')
    assert store.search('zebras migrate', [saved['id']])[1]
    with pytest.raises(ValueError):
        store.import_document('notes.txt', b'\xff\xfe', replace=True)
    assert store.search('zebras migrate', [saved['id']])[1]
    replacement=store.import_document('notes.txt', b'beta dolphins eat fish.', replace=True)
    assert replacement['id']==saved['id']
    assert not store.search('zebras migrate', [saved['id']])[1]
    assert store.search('dolphins fish', [saved['id']])[1]


def test_delete_removes_index_and_cached_quotations(isolated_omm_home):
    store=DocumentStore(); saved=store.import_document('secret.txt', b'private needle document')
    revision,sources=store.search('private needle',[saved['id']])
    store.save_answer({'id':'answer','created_at':'2026-10-19','sources':sources},revision)
    assert DocumentStore().answers()
    store.delete(saved['id'])
    assert DocumentStore().list()==[] and DocumentStore().answers()==[]
    with pytest.raises(ValueError):store.search('private needle',[saved['id']])


def test_changed_corpus_refuses_late_answer(isolated_omm_home):
    store=DocumentStore(); saved=store.import_document('notes.txt',b'alpha zebra')
    revision,_=store.search('alpha zebra',[saved['id']]);store.delete(saved['id'])
    with pytest.raises(ValueError,match='변경'):
        store.save_answer({'id':'answer','created_at':'2026-10-19'},revision)


@pytest.mark.parametrize('name,content',[('../escape.txt',b'ok'),('foo.html',b'<script>alert(1)</script><style>p{}</style>'),('legacy.hwp',b'HWP Document File'),('broken.hwpx',b'PK not a zip'),('photo.png',b'png'),('binary.txt',b'\x00hello'),('empty.md',b'  '),('invalid.txt',b'\xff'),('bad.pdf',b'not PDF')])
def test_invalid_document_is_not_added(isolated_omm_home,name,content):
    with pytest.raises(ValueError):DocumentStore().import_document(name,content)
    assert DocumentStore().list()==[]


def test_blank_and_encrypted_pdf_are_not_success(isolated_omm_home):
    writer=PdfWriter();writer.add_blank_page(width=200,height=200)
    stream=io.BytesIO();writer.write(stream)
    with pytest.raises(ValueError,match='OCR'):extract('scan.pdf',stream.getvalue())
    writer.encrypt('secret');stream=io.BytesIO();writer.write(stream)
    with pytest.raises(ValueError,match='암호'):extract('locked.pdf',stream.getvalue())


def test_pdf_without_pypdf_explains_reinstall(isolated_omm_home,monkeypatch):
    import builtins
    real_import=builtins.__import__
    def blocked(name,*args,**kwargs):
        if name=='pypdf' or name.startswith('pypdf.'):raise ImportError('blocked')
        return real_import(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',blocked)
    with pytest.raises(ValueError,match='pypdf') as error:extract('doc.pdf',b'%PDF-1.4 data')
    assert '다시 설치' in str(error.value)
    assert DocumentStore().list()==[]


def test_pypdf_is_a_runtime_dependency():
    import re
    from pathlib import Path
    text=(Path(__file__).resolve().parents[1]/'pyproject.toml').read_text(encoding='utf-8')
    block=re.search(r'^dependencies = \[(.*?)^\]',text,re.M|re.S).group(1)
    assert '"pypdf>=' in block


def test_text_pdf_keeps_page_numbers(isolated_omm_home):
    # Minimal real PDF text objects; extraction is not mocked.
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer=PdfWriter()
    for text in ('First page compass.', 'Second page magnetic experiment.'):
        page=writer.add_blank_page(width=300,height=300)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        content=DecodedStreamObject();content.set_data(f'BT /F1 12 Tf 10 250 Td ({text}) Tj ET'.encode())
        page[NameObject('/Contents')]=writer._add_object(content)
    stream=io.BytesIO();writer.write(stream)
    saved=DocumentStore().import_document('experiment.pdf',stream.getvalue())
    _,sources=DocumentStore().search('magnetic experiment',[saved['id']])
    assert sources[0]['page']==2 and 'magnetic experiment' in sources[0]['text']


def test_symlinked_store_refused(isolated_omm_home,tmp_path,requires_symlink_support):
    other=tmp_path/'other';other.mkdir();(isolated_omm_home/'documents').symlink_to(other,target_is_directory=True)
    with pytest.raises(ValueError):DocumentStore().list()


def test_symlinked_database_refused(isolated_omm_home,tmp_path,requires_symlink_support):
    DocumentStore().list();other=tmp_path/'outside';other.write_text('untouched',encoding='utf-8')
    path=isolated_omm_home/'documents/library.sqlite3';path.unlink();path.symlink_to(other)
    with pytest.raises(ValueError):DocumentStore().list()
    assert other.read_text(encoding='utf-8')=='untouched'


@pytest.mark.parametrize('bad',[{}, {'confirmed':True,'request_id':'x','name':'x.txt','content':'a','path':'/private'}, {'confirmed':True,'request_id':'x','name':'x.txt','content':'a','replace':'yes'}])
def test_upload_contract_rejects_extra_paths_and_bad_types(bad):
    with pytest.raises(ValueError):validate('/api/documents',bad,None)


def test_model_reference_cannot_be_supplied_as_path():
    class Service:
        def models(self):return []
    body={'confirmed':True,'request_id':'x','question':'a question','document_ids':['id'],'model_id':'/tmp/model','engine':'ollama'}
    with pytest.raises(ValueError):validate('/api/documents/question',body,Service())


def test_no_evidence_does_not_contact_a_model(isolated_omm_home,monkeypatch):
    from omm.web import document_questions as q
    saved=DocumentStore().import_document('notes.txt',b'magnetic compass copper experiment')
    monkeypatch.setattr(q,'adapter_for',lambda *a:pytest.fail('no model contact without evidence'))
    result=q.answer({'question':'dinosaurs asteroid extinction','document_ids':[saved['id']],'filename':'none.gguf','engine':'ollama'},threading.Event())
    assert result['status']=='no_evidence' and DocumentStore().answers()[0]==result


@pytest.fixture
def runtime_question(isolated_omm_home,monkeypatch):
    from types import SimpleNamespace as NS
    from omm import registry
    from omm.web import document_questions as q
    store=DocumentStore();saved=store.import_document('notes.txt',b'The library loan duration is seventeen days.')
    (config.MODELS_DIR/'test.gguf').write_bytes(b'GGUF-test')
    registry.save_registry({'test.gguf':{'ollama_name':'test','linked':{'ollama':True}}})
    receipt=NS(was_already_loaded=False)
    class Adapter:
        loaded=False; released=False; failure=False; text='Loan duration is seventeen days [1].'
        def health(self):return NS(reachable=True)
        def list_models(self):return [NS(loaded=self.loaded)]
        def load(self,*args):return receipt
        def generate(self,*args):
            if self.failure:raise RuntimeError('generation failed')
            return NS(text=self.text)
        def unload(self,*args):self.released=True;return NS(unloaded=True)
    adapter=Adapter()
    monkeypatch.setattr(q,'adapter_for',lambda *a:adapter)
    monkeypatch.setattr(q.hardware,'scan_hardware',lambda:None)
    monkeypatch.setattr(q.memory_guard,'plan_memory_guard',lambda *a:NS(decision=q.memory_guard.GuardDecision.SAFE))
    return q,adapter,{'question':'library loan duration','document_ids':[saved['id']],'filename':'test.gguf','engine':'ollama'}


def test_answer_has_exact_citation_and_releases_owned_model(runtime_question):
    q,adapter,request=runtime_question
    result=q.answer(request,threading.Event())
    assert result['status']=='answered' and adapter.released
    assert result['sources'][0]['text']=='The library loan duration is seventeen days.'
    assert DocumentStore().answers()[0]==result


def test_generation_failure_releases_owned_model(runtime_question):
    q,adapter,request=runtime_question;adapter.failure=True
    with pytest.raises(RuntimeError):q.answer(request,threading.Event())
    assert adapter.released and DocumentStore().answers()==[]


@pytest.mark.parametrize('text',['unsupported fact','unsupported fact [99]'])
def test_invented_or_missing_citation_not_presented_as_answer(runtime_question,text):
    q,adapter,request=runtime_question;adapter.text=text
    result=q.answer(request,threading.Event())
    assert result['status']=='unsupported_answer' and result['answer']!=text
    assert adapter.released


def test_preloaded_runtime_is_preserved(runtime_question):
    q,adapter,request=runtime_question;adapter.loaded=True
    with pytest.raises(ValueError,match='로딩'):q.answer(request,threading.Event())
    assert not adapter.released and DocumentStore().answers()==[]


def test_generic_job_result_excludes_document_question_and_quotes(monkeypatch):
    from omm.web import worker, document_questions
    monkeypatch.setattr(document_questions,'answer',lambda *_:{'id':'answer-id','status':'answered','question':'PRIVATE_QUERY','sources':[{'text':'PRIVATE_QUOTATION'}],'answer':'PRIVATE_ANSWER'})
    result=worker.execute({'operation':'document_question'},threading.Event())
    assert result=={'answer_id':'answer-id','status':'answered','runtime_response_saved':True}
    assert 'PRIVATE' not in json.dumps(result)


def test_legacy_store_without_index_metadata_is_stamped_with_first_method(isolated_omm_home):
    import sqlite3
    root=isolated_omm_home/'documents';root.mkdir(parents=True)
    db=sqlite3.connect(root/'library.sqlite3')
    db.executescript("""CREATE TABLE documents (id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, sha256 TEXT NOT NULL,
        size_bytes INTEGER NOT NULL, pages INTEGER NOT NULL, imported_at TEXT NOT NULL);
        CREATE TABLE chunks (document_id TEXT NOT NULL, page INTEGER NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL, text TEXT NOT NULL);
        INSERT INTO documents VALUES('old','old.txt','x',10,1,'2026-10-01');
        INSERT INTO chunks VALUES('old',1,0,24,'zebra migration notes ok');""")
    db.commit();db.close()
    row=DocumentStore().list()[0]
    assert row['retrieval']=='local_lexical_v1' and row['chunking']=='chunk900_overlap100_v1' and row['index_current']
    assert DocumentStore().search('zebra migration',['old'])[1]


def test_index_method_mismatch_refuses_search_until_reregistered(isolated_omm_home,monkeypatch):
    from omm import documents
    store=DocumentStore();content=b'zebra migration happens in the dry season'
    saved=store.import_document('zebra.txt',content)
    monkeypatch.setattr(documents,'CHUNKING_VERSION','chunk_test_v2')
    assert DocumentStore().list()[0]['index_current'] is False
    with pytest.raises(ValueError,match='다시 등록') as error:DocumentStore().search('zebra migration',[saved['id']])
    assert 'zebra.txt' in str(error.value)
    # Same bytes are not "unchanged" while the index is stale: they are re-chunked.
    again=DocumentStore().import_document('zebra.txt',content,replace=True)
    assert again.get('unchanged') is None and again['id']==saved['id']
    row=DocumentStore().list()[0]
    assert row['chunking']=='chunk_test_v2' and row['index_current']
    assert DocumentStore().search('zebra migration',[saved['id']])[1][0]['text']==content.decode()


def _select(filename,engine='ollama'):
    from omm.web import document_settings
    return document_settings.select('id-'+filename,filename,engine,[])


def test_model_change_keeps_documents_and_failed_new_model_rolls_back(runtime_question):
    from omm import registry
    from omm.web import document_settings
    q,adapter,request=runtime_question
    _select('test.gguf')
    assert q.answer(request,threading.Event())['status']=='answered'
    assert document_settings.load()['confirmed']['filename']=='test.gguf'
    (config.MODELS_DIR/'new.gguf').write_bytes(b'GGUF-new')
    registry.upsert_entry('new.gguf',ollama_name='new',linked={'ollama':True})
    change=_select('new.gguf')['change']
    assert 'test.gguf → new.gguf' in change and '그대로' in change
    documents_before=DocumentStore().list();answers_before=DocumentStore().answers()
    adapter.failure=True
    with pytest.raises(ValueError,match='되돌렸어요'):q.answer({**request,'filename':'new.gguf'},threading.Event())
    settings=document_settings.load()
    assert settings['selected']['filename']=='test.gguf'
    assert DocumentStore().list()==documents_before and DocumentStore().answers()==answers_before
    assert (config.MODELS_DIR/'new.gguf').read_bytes()==b'GGUF-new' and (config.MODELS_DIR/'test.gguf').is_file()
    # The previous model still answers from the same stored index.
    adapter.failure=False
    assert q.answer(request,threading.Event())['sources'][0]['text']=='The library loan duration is seventeen days.'


def test_failure_without_a_previous_answering_model_is_not_rolled_back(runtime_question):
    from omm.web import document_settings
    q,adapter,request=runtime_question
    _select('test.gguf');adapter.failure=True
    with pytest.raises(RuntimeError):q.answer(request,threading.Event())
    assert document_settings.load()['selected']['filename']=='test.gguf' and document_settings.load()['confirmed'] is None


HTML_SAMPLE = """<!doctype html><html><head><meta charset="utf-8"><title>실험 안내</title>
<style>.secret{color:red}</style><script>var hidden = "스크립트 비밀";</script></head>
<body><h1>자기장 측정</h1><p>나침반과   구리 코일을 준비합니다.</p>
<ul><li>전류는 &lt;2A&gt;로 제한합니다.</li><li>측정값은 표에 적습니다.</li></ul><noscript>자바스크립트 필요</noscript></body></html>"""


def test_html_text_skips_script_and_style_and_quotes_match(isolated_omm_home):
    pages=extract('guide.html',HTML_SAMPLE.encode('utf-8'))
    text=pages[0][1]
    assert '나침반과 구리 코일을 준비합니다.' in text and '전류는 <2A>로 제한합니다.' in text
    assert '스크립트 비밀' not in text and 'color:red' not in text and '자바스크립트 필요' not in text
    saved=DocumentStore().import_document('guide.html',HTML_SAMPLE.encode('utf-8'))
    _,sources=DocumentStore().search('구리 코일 준비',[saved['id']])
    assert sources and sources[0]['text']==text[sources[0]['start']:sources[0]['end']]


def test_html_declared_legacy_korean_encoding_is_decoded(isolated_omm_home):
    content='<html><head><meta charset="euc-kr"></head><body><p>도서관 대출 기간은 십칠 일입니다.</p></body></html>'.encode('euc-kr')
    assert extract('old.htm',content)==[(1,'도서관 대출 기간은 십칠 일입니다.')]


def _hwpx(sections):
    import zipfile
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as archive:
        archive.writestr('mimetype','application/hwp+zip')
        for number,body in enumerate(sections):
            archive.writestr(f'Contents/section{number}.xml',
                '<?xml version="1.0" encoding="UTF-8"?><hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
                'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'+body+'</hs:sec>')
    return stream.getvalue()


def _p(*runs):
    return '<hp:p>'+''.join(f'<hp:run><hp:t>{text}</hp:t></hp:run>' for text in runs)+'</hp:p>'


def test_hwpx_paragraphs_and_table_cells_are_extracted_in_order(isolated_omm_home):
    table='<hp:p><hp:run><hp:tbl><hp:tr><hp:tc><hp:subList>'+_p('표 안의 준비물: 자석')+'</hp:subList></hp:tc></hp:tr></hp:tbl></hp:run></hp:p>'
    content=_hwpx([_p('첫 구역 ','실험 목표는 자기장 측정입니다.')+table, _p('둘째 구역 결과 정리 방법')])
    text=extract('report.hwpx',content)[0][1]
    assert text.split('\n')==['첫 구역 실험 목표는 자기장 측정입니다.','표 안의 준비물: 자석','둘째 구역 결과 정리 방법']
    saved=DocumentStore().import_document('report.hwpx',content)
    _,sources=DocumentStore().search('자기장 측정 목표',[saved['id']])
    assert sources and sources[0]['text']==text[sources[0]['start']:sources[0]['end']]


@pytest.mark.parametrize('content',[
    lambda: _hwpx([_p('   ')]),
    lambda: _hwpx([]),
    lambda: _hwpx(['<!DOCTYPE x [<!ENTITY a "aaaa">]><hp:p>&a;</hp:p>']),
])
def test_hwpx_without_body_text_is_not_success(isolated_omm_home,content):
    with pytest.raises(ValueError):DocumentStore().import_document('empty.hwpx',content())
    assert DocumentStore().list()==[]
