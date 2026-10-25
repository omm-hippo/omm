import test from 'node:test';
import assert from 'node:assert/strict';
import {recoveryActions} from '../src/recovery-state.js';

test('typed recovery actions never delete, bypass safety, or start unsupported runtimes',()=>{
  assert.equal(recoveryActions('server_unavailable','ollama')[0].id,'start_ollama');
  assert.deepEqual(recoveryActions('server_unavailable','lmstudio').map(x=>x.id),['connections']);
  assert.equal(recoveryActions('out_of_memory')[0].id,'smaller');
  assert.equal(recoveryActions('disk_full')[0].id,'storage');
  assert.equal(recoveryActions('safety_failed')[0].id,'diagnostics');
  assert.equal(recoveryActions('arbitrary shell command')[0].id,'diagnostics');
});
