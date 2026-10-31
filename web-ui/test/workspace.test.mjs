import test from 'node:test';
import assert from 'node:assert/strict';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {createServer} from 'vite';
import {searchDestinations,readAppearance,shouldSend,measurementSummary} from '../src/workspace-state.js';

// Composition Enter commits Korean/Japanese input; it must never submit a chat.
test('composition and multiline shortcuts never submit a message',()=>{
  assert.equal(shouldSend({key:'Enter',nativeEvent:{isComposing:true}}),false);
  assert.equal(shouldSend({key:'Enter',keyCode:229}),false);
  assert.equal(shouldSend({key:'Enter',shiftKey:true}),false);
  assert.equal(shouldSend({key:'Enter',altKey:true}),false);
  assert.equal(shouldSend({key:'Enter',ctrlKey:true}),true);
  assert.equal(shouldSend({key:'a'}),false);
});

test('menu search finds a task by intent without executing it',()=>{
  assert.equal(searchDestinations('근거 질문')[0].id,'documents');
  assert.ok(searchDestinations('서버').some(item=>item.id==='connections'));
  assert.equal(searchDestinations('공유 토큰')[0].id,'shared');
  assert.equal(searchDestinations('없는 메뉴').length,0);
});

test('unavailable or corrupt browser storage does not prevent startup',()=>{
  const previous=Object.getOwnPropertyDescriptor(globalThis,'localStorage');
  try {
    Object.defineProperty(globalThis,'localStorage',{configurable:true,get(){throw new Error('blocked');}});
    assert.equal(readAppearance(),'system');
    Object.defineProperty(globalThis,'localStorage',{configurable:true,value:{getItem:()=>'<bad>'}});
    assert.equal(readAppearance(),'system');
    Object.defineProperty(globalThis,'localStorage',{configurable:true,value:{getItem:()=> 'dark'}});
    assert.equal(readAppearance(),'dark');
  } finally {
    if(previous)Object.defineProperty(globalThis,'localStorage',previous);else delete globalThis.localStorage;
  }
});

test('answers render GFM tables and code without remote images or executable markup',async()=>{
  const server=await createServer({configFile:false,root:new URL('..',import.meta.url).pathname,server:{middlewareMode:true},appType:'custom'});
  try {
    const {default:RichText}=await server.ssrLoadModule('/src/RichText.jsx');
    const output=renderToStaticMarkup(createElement(RichText,{text:'## 확인\n\n| 이름 | 값 |\n| --- | --- |\n| 한글 | 7 |\n\n```python\nprint(7)\n```\n\n[위험](javascript:alert(1))\n\n![외부 그림](https://example.invalid/track.png)\n\n<script>alert(1)</script>\n<img src=x onerror=alert(1)>'}));
    assert.match(output,/<h2>확인<\/h2>/);
    assert.match(output,/<table>/);
    assert.match(output,/코드 복사/);
    assert.match(output,/print\(7\)/);
    assert.doesNotMatch(output,/<img|<script|javascript:|onerror|track\.png/);
  } finally {await server.close();}
});

test('recommendation evidence keeps counts separate and never invents accuracy',()=>{
  const text=measurementSummary({raw_rows:488,valid_rows:331,repeat_samples_collapsed:98,distinct_configurations:233,independent_devices:null});
  assert.match(text,/원시 측정 488행\(유효 331행\)/);
  assert.match(text,/반복 측정 병합 98회/);
  assert.match(text,/서로 다른 구성 233개/);
  assert.match(text,/독립 장비 수 확인 불가/);
  assert.doesNotMatch(text,/%/);
  assert.match(measurementSummary({}),/원시 측정 확인 불가\(유효 확인 불가\)/);
});

