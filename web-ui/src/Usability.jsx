import React from 'react';
import {recoveryActions} from './recovery-state.js';
import {L} from './i18n.js';

export function RecoveryActions({reason,engine,onRecover}) {
  return <div className="recovery-actions" aria-label={L('문제 해결','Troubleshooting')}>{recoveryActions(reason,engine).map(action=><button key={action.id} onClick={()=>onRecover(action.id)}>{action.label}</button>)}</div>;
}
