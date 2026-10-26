import test from 'node:test';
import assert from 'node:assert/strict';
import {documentProgress} from '../src/document-progress.js';

const job=fields=>({operation:'document_import',status:'running',...fields});

test('document import shows extraction, then embedding pieces with a bar',()=>{
  assert.deepEqual(documentProgress(job({document_stage:'extract',completed_count:0,total_count:0})),{label:'텍스트 추출 중',value:null,max:null});
  assert.deepEqual(documentProgress(job({document_stage:'embed',completed_count:32,total_count:73})),{label:'의미 색인 만드는 중 32/73 조각',value:32,max:73});
  assert.equal(documentProgress(job({document_stage:'embed',completed_count:99,total_count:73})).value,73);
  assert.equal(documentProgress(job({status:'queued'})).label,'등록 대기 중');
  assert.deepEqual(documentProgress(job({document_stage:'upload',completed_count:0,total_count:0})),{label:'회사 head로 보내고 색인 만드는 중',value:null,max:null});
});

test('finished or unrelated jobs keep today\'s final rendering',()=>{
  for(const status of ['completed','failed','cancelled','interrupted'])
    assert.equal(documentProgress(job({status,document_stage:'embed',completed_count:73,total_count:73})),null);
  assert.equal(documentProgress({operation:'compare',status:'running',completed_count:1,total_count:2}),null);
  assert.equal(documentProgress(null),null);
});
