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


@pytest.mark.parametrize('name,content',[('../escape.txt',b'ok'),('foo.html',b'ok'),('binary.txt',b'\x00hello'),('empty.md',b'  '),('invalid.txt',b'\xff'),('bad.pdf',b'not PDF')])
def test_invalid_document_is_not_added(isolated_omm_home,name,content):
    with pytest.raises(ValueError):DocumentStore().import_document(name,content)
    assert DocumentStore().list()==[]


def test_blank_and_encrypted_pdf_are_not_success(isolated_omm_home):
    writer=PdfWriter();writer.add_blank_page(width=200,height=200)
    stream=io.BytesIO();writer.write(stream)
    with pytest.raises(ValueError,match='OCR'):extract('scan.pdf',stream.getvalue())
    writer.encrypt('secret');stream=io.BytesIO();writer.write(stream)
    with pytest.raises(ValueError,match='암호'):extract('locked.pdf',stream.getvalue())


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
