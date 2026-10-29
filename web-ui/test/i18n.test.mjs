import test from 'node:test';
import assert from 'node:assert/strict';
import {detectLanguage,L,lang} from '../src/i18n.js';

test('a saved choice wins over the browser language', () => {
  assert.equal(detectLanguage('en','ko-KR'),'en');
  assert.equal(detectLanguage('ko','en-US'),'ko');
});

test('without a saved choice the browser language decides', () => {
  assert.equal(detectLanguage(null,'ko-KR'),'ko');
  assert.equal(detectLanguage(null,'en-US'),'en');
  assert.equal(detectLanguage('fr','ja-JP'),'en');
  assert.equal(detectLanguage(null,undefined),'ko');
});

test('outside a browser the UI stays Korean', () => {
  assert.equal(lang,'ko');
  assert.equal(L('한국어','English'),'한국어');
});
