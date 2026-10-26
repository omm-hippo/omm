import test from 'node:test';
import assert from 'node:assert/strict';
import {sharedMessages} from '../src/shared-ai-state.js';

test('recent turns retain complete user/assistant pairs and the new question',()=>{
  const turns=Array.from({length:20},(_,i)=>({user:`question ${i}`,assistant:`answer ${i}`}));
  const messages=sharedMessages(turns,'new question');
  assert.equal(messages.length,25);
  assert.equal(messages[0].content,'question 8');
  assert.deepEqual(messages.at(-1),{role:'user',content:'new question'});
  assert.ok(messages.every((item,i)=>item.role===(i%2===0?'user':'assistant')));
});

test('Korean conversation is bounded by encoded bytes without cutting a turn',()=>{
  const turns=Array.from({length:12},(_,i)=>({user:'질문'.repeat(1500)+i,assistant:'답변'.repeat(2000)+i}));
  const question='최근 질문'.repeat(500);
  const messages=sharedMessages(turns,question);
  assert.ok(new TextEncoder().encode(JSON.stringify(messages)).byteLength<=45*1024);
  assert.ok(messages.length<25 && messages.length%2===1);
  assert.equal(messages.at(-1).content,question);
  assert.ok(messages.at(-2).content.endsWith('11'));
});
