import React, {useEffect, useRef, useState} from 'react';
import {api} from './api';
import {CopyButton} from './RichText';
import {L} from './i18n.js';

// Sharing pieces of the 내 문서 tab. The employee token never reaches this
// page: the local server keeps it and answers only with the head address and
// the token name. A newly issued token is the one exception, shown once.

export function LocationBar({rag,onConnect,onLocal,busy}){
  const remote=rag?.mode==='remote';
  return <section className="rag-location" aria-label={L('지금 보는 곳','Viewing')}>
    <div><span className="rag-location-label">📍 {L('지금 보는 곳','Viewing')}</span>
      <strong>{!rag?L('확인 중…','Checking…'):remote?L(`회사 head${rag.remote?.url?` (${rag.remote.url})`:''}`,`Company head${rag.remote?.url?` (${rag.remote.url})`:''}`):L('이 PC','This PC')}</strong>
      {remote && rag.remote?.name && <small>{L('나','Me')}: {rag.remote.name}</small>}
      {!remote && rag && <small>{L('이 PC의 문서 저장소 · 관리자','This PC’s document store · admin')}</small>}
      {rag?.semantic_search && !rag.semantic_search.ready && <small className="warning" role="status">{rag.semantic_search.text}</small>}
      {rag?.semantic_search?.persistence && <small>{rag.semantic_search.persistence}</small>}
    </div>
    {rag && <div className="row-actions"><button disabled={busy} onClick={onConnect}>{remote?L('다른 출입증으로 연결','Connect with another token'):L('회사에 연결','Connect to company')}</button>{remote && <button disabled={busy} onClick={onLocal}>{L('이 PC로 돌아가기','Back to this PC')}</button>}</div>}
  </section>;
}

export function ConnectDialog({onClose,onDone}){
  const dialog=useRef(null);
  const [url,setUrl]=useState('');
  const [token,setToken]=useState('');
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  useEffect(()=>{dialog.current?.showModal();},[]);
  async function submit(event){
    event.preventDefault();setBusy(true);setError('');
    try{const value=await api('/api/rag/remote/set',{url,token});setToken('');onDone(value);}
    catch(e){setError(e.message);}
    finally{setBusy(false);}
  }
  return <dialog ref={dialog} onCancel={event=>{if(busy)event.preventDefault();else onClose();}}><form onSubmit={submit}>
    <h2>{L('회사에 연결','Connect to company')}</h2>
    <p>{L('관리자에게 받은 회사 head 주소와 직원 출입증(토큰)을 넣어 주세요. 연결하기 전에 head에 토큰을 보여 주고 누구인지 확인해요.','Enter the company head address and employee token from your admin. Before connecting, OMM shows the token to the head to confirm who you are.')}</p>
    {error && <p role="alert" className="error">{error}</p>}
    <label>{L('회사 head 주소','Company head address')}<input className="rag-input" value={url} disabled={busy} onChange={e=>setUrl(e.target.value)} placeholder={L('예: 192.168.0.10 또는 http://head.local:52440','e.g. 192.168.0.10 or http://head.local:52440')} autoComplete="off" required/></label>
    <label>{L('직원 출입증(토큰)','Employee token')}<input className="rag-input" type="password" value={token} disabled={busy} onChange={e=>setToken(e.target.value)} placeholder="omm_..." autoComplete="off" required/></label>
    <p className="footnote">{L('토큰은 이 PC의 OMM 폴더(rag-remote.json)에만 저장하고 화면에 다시 보여 주지 않아요.','The token is stored only in this PC’s OMM folder (rag-remote.json) and is never shown again.')}</p>
    <div className="dialog-actions"><button type="button" disabled={busy} onClick={onClose}>{L('취소','Cancel')}</button><button className="primary" disabled={busy || !url.trim() || !token.trim()}>{busy?L('확인 중…','Checking…'):L('확인하고 연결','Check and connect')}</button></div>
  </form></dialog>;
}

function AddTarget({kind,options,onAdd,busy}){
  const [open,setOpen]=useState(false);
  const [value,setValue]=useState('');
  const list=`rag-${kind}-options`;
  if(!open)return <button disabled={busy} onClick={()=>setOpen(true)}>{kind==='person'?L('사람 추가','Add person'):L('팀 추가','Add team')}</button>;
  return <form className="rag-inline-form" onSubmit={async e=>{e.preventDefault();if(await onAdd(kind,value.trim())){setValue('');setOpen(false);}}}>
    <input className="rag-input" aria-label={kind==='person'?L('직원 이름(토큰 이름)','Employee name (token name)'):L('팀 이름','Team name')} list={options?.length?list:undefined} value={value} disabled={busy} onChange={e=>setValue(e.target.value)} placeholder={kind==='person'?L('직원 이름(토큰 이름)','Employee name (token name)'):L('팀 이름','Team name')} autoFocus/>
    {options?.length>0 && <datalist id={list}>{options.map(x=><option key={x} value={x}/>)}</datalist>}
    <button className="primary" disabled={busy || !value.trim()}>{L('공유','Share')}</button><button type="button" disabled={busy} onClick={()=>setOpen(false)}>{L('취소','Cancel')}</button>
  </form>;
}

export function SharePanel({collection,people,teams,busy,onChange,onError}){
  const shares=collection.shares || [];
  async function add(kind,target){
    try{const value=await api('/api/rag/share',{collection:collection.name,kind,target});onChange(value.added?L(`${kind==='team'?'팀 ':''}${target}에게 공유했어요. 이제 검색할 수 있어요.`,`Shared with ${kind==='team'?'team ':''}${target}. They can search it now.`):L(`이미 ${kind==='team'?'팀 ':''}${target}에게 공유돼 있어요.`,`Already shared with ${kind==='team'?'team ':''}${target}.`));return true;}
    catch(e){onError(e.message);return false;}
  }
  async function remove(share){
    const label=L(`${share.kind==='team'?'팀 ':''}${share.target}`,`${share.kind==='team'?'team ':''}${share.target}`);
    if(!window.confirm(L(`'${collection.name}' 컬렉션 공유를 ${label}에게서 거둘까요? 바로 검색할 수 없게 돼요.`,`Stop sharing the '${collection.name}' collection with ${label}? They will lose search access right away.`)))return;
    try{const value=await api('/api/rag/unshare',{collection:collection.name,kind:share.kind,target:share.target,confirmed:true});onChange(value.removed?L(`${label}에게서 공유를 거뒀어요.`,`Stopped sharing with ${label}.`):L(`${label}에게 공유돼 있지 않았어요. 바뀐 것은 없어요.`,`It wasn’t shared with ${label}. Nothing changed.`));}
    catch(e){onError(e.message);}
  }
  return <section className="rag-share"><h3>{L('공유','Sharing')}</h3>
    <p className="footnote">{L('만든 사람과 관리자는 늘 검색할 수 있어요. 여기서 정한 사람·팀만 더 볼 수 있어요.','The creator and admin can always search it. Only the people and teams you add here can also see it.')}</p>
    <div className="rag-chips">{!shares.length && <span className="footnote">{L('아직 아무에게도 공유하지 않았어요.','Not shared with anyone yet.')}</span>}{shares.map(s=><span className="rag-chip" key={s.kind+s.target}><span aria-hidden="true">{s.kind==='team'?'👥':'👤'}</span>{s.kind==='team'?L('팀 ','Team '):''}{s.target}<button disabled={busy} aria-label={L(`${s.target} 공유 거두기`,`Stop sharing with ${s.target}`)} onClick={()=>remove(s)}>✕</button></span>)}</div>
    <div className="row-actions rag-share-actions"><AddTarget kind="person" options={people} onAdd={add} busy={busy}/><AddTarget kind="team" options={teams} onAdd={add} busy={busy}/></div>
  </section>;
}

const CONNECTION_STATE={added:L('연결했어요.','Connected.'),updated:L('연결 설정을 새로 고쳤어요.','Updated the connection settings.'),unchanged:L('이미 연결돼 있어요.','Already connected.')};
const DISCONNECT_STATE={restored:L('원래 설정으로 되돌렸어요.','Restored the original settings.'),removed:L('OMM 항목만 지우고 다른 설정은 그대로 두었어요.','Removed only the OMM entry. Other settings were left as is.'),absent:L('OMM 항목이 이미 없었어요. 바뀐 것은 없어요.','The OMM entry was already gone. Nothing changed.'),not_connected:L('OMM이 연결한 적이 없어요. 바뀐 것은 없어요.','OMM never connected this. Nothing changed.')};

export function AgentSection({rag,onRefresh}){
  const [busy,setBusy]=useState('');
  const [message,setMessage]=useState('');
  const [error,setError]=useState('');
  const remote=rag?.mode==='remote';
  async function act(row,removing){
    if(removing && !window.confirm(L(`${row.label}에서 OMM 문서 검색 연결을 해제할까요? 다른 설정은 그대로 둬요.`,`Disconnect OMM document search from ${row.label}? Other settings stay as they are.`)))return;
    setBusy(row.harness);setError('');setMessage('');
    try{
      const value=await api(removing?'/api/rag/disconnect':'/api/rag/connect',removing?{harness:row.harness,confirmed:true}:{harness:row.harness});
      setMessage(removing?`${row.label}: ${DISCONNECT_STATE[value.state]||L('연결을 해제했어요.','Disconnected.')}`:L(`${row.label}: ${CONNECTION_STATE[value.state]||'연결했어요.'} ${row.label}을(를) 다시 시작해야 문서 검색 도구가 보여요.${row.harness==='claude-code'?' Claude Code가 켜져 있으면 완전히 끄고 다시 켜 주세요.':''}${value.remote?' 직원 토큰이 그 도구의 설정 파일에 저장돼요. 파일을 공유하거나 올리지 마세요.':''}`,`${row.label}: ${CONNECTION_STATE[value.state]||'Connected.'} Restart ${row.label} to see the document search tools.${row.harness==='claude-code'?' If Claude Code is open, quit it completely and open it again.':''}${value.remote?' The employee token is saved in that tool’s settings file. Don’t share or upload that file.':''}`));
      await onRefresh();
    }catch(e){setError(e.message);}
    finally{setBusy('');}
  }
  return <section className="manager-section rag-section"><h2>{L('AI 연결','AI connections')}</h2>
    <p>{L(`코딩 에이전트가 ${remote?'회사 head에서 나에게 공유된 문서':'이 PC의 문서'}를 근거로 찾을 수 있게 연결해요. 원문 인용과 쪽 번호를 함께 받아요.`,`Connect coding agents so they can search ${remote?'documents shared with you on the company head':'documents on this PC'} as sources. Results include quotes from the original and page numbers.`)}</p>
    {error && <p role="alert" className="error">{error}</p>}{message && <p className="notice" role="status">{message}</p>}
    <div className="connection-list">{(rag?.harnesses||[]).map(row=>{
      const where=row.registered?(row.remote?L('회사 head 문서','company head documents'):L('이 PC 문서','this PC’s documents')):null;
      return <article key={row.harness}><div><h3>{row.label}</h3>
        <p>{row.error?row.error:!row.installed && !row.registered?L('이 PC에서 찾지 못했어요(설치되지 않음)','Not found on this PC (not installed)'):row.registered?L(`연결됨 · ${where}`,`Connected · ${where}`):L('연결 안 됨','Not connected')}</p>
        {row.registered && where && ((row.remote && !remote) || (!row.remote && remote)) && <small className="warning">{L('지금 보는 곳과 다른 곳에 연결돼 있어요. 다시 [연결]하면 지금 보는 곳으로 바꿔요.','Connected to a different place than the one you’re viewing. Click [Connect] again to switch it here.')}</small>}
        {row.registered && !row.managed && <small>{L('OMM이 만든 항목이 아니에요. 직접 넣은 설정은 건드리지 않아요.','This entry wasn’t made by OMM. Settings you added yourself are left alone.')}</small>}
      </div><div className="row-actions">
        {(row.installed || row.registered) && !row.error && (!row.registered || (row.managed && ((row.remote && !remote) || (!row.remote && remote) || (!row.remote && !row.current)))) && <button className="primary" disabled={!!busy} onClick={()=>act(row,false)}>{busy===row.harness?L('연결 중…','Connecting…'):L('연결','Connect')}</button>}
        {row.registered && row.managed && <button disabled={!!busy} onClick={()=>act(row,true)}>{busy===row.harness?L('해제 중…','Disconnecting…'):L('해제','Disconnect')}</button>}
      </div></article>;})}</div>
  </section>;
}

export function TeamSection({onPeople}){
  const [data,setData]=useState({teams:[],people:[]});
  const [name,setName]=useState('');
  const [member,setMember]=useState({});
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [message,setMessage]=useState('');
  async function refresh(){const value=await api('/api/rag/teams');setData(value);onPeople?.(value);return value;}
  useEffect(()=>{refresh().catch(e=>setError(e.message));},[]);
  async function call(path,body,done){
    setBusy(true);setError('');setMessage('');
    try{const value=await api(path,body);setMessage(done(value));await refresh();return true;}
    catch(e){setError(e.message);return false;}
    finally{setBusy(false);}
  }
  return <section className="manager-section rag-section"><h2>{L('팀 관리','Teams')}</h2>
    <p>{L('팀을 만들어 직원(출입증 이름)을 넣으면 컬렉션을 팀 단위로 공유할 수 있어요. 팀은 이 PC(관리자)에서만 관리해요.','Create a team and add employees (token names) to share collections with the whole team. Teams are managed only on this PC (admin).')}</p>
    {error && <p role="alert" className="error">{error}</p>}{message && <p className="notice" role="status">{message}</p>}
    <form className="rag-inline-form" onSubmit={async e=>{e.preventDefault();if(await call('/api/rag/teams/create',{name:name.trim()},()=>L(`'${name.trim()}' 팀을 만들었어요. 직원을 넣어 주세요.`,`Created team '${name.trim()}'. Now add employees.`)))setName('');}}>
      <input className="rag-input" aria-label={L('새 팀 이름','New team name')} value={name} disabled={busy} onChange={e=>setName(e.target.value)} placeholder={L('새 팀 이름 (예: sales)','New team name (e.g. sales)')}/><button className="primary" disabled={busy || !name.trim()}>{L('팀 만들기','Create team')}</button>
    </form>
    {!data.teams.length && <p className="footnote">{L('아직 팀이 없어요.','No teams yet.')}</p>}
    <div className="rag-team-list">{data.teams.map(team=><article key={team.name}>
      <div className="rag-team-heading"><h3>👥 {team.name}</h3><button className="danger" disabled={busy} onClick={()=>{if(window.confirm(L(`'${team.name}' 팀을 지울까요? 이 팀에 준 공유도 모두 거둬요.`,`Delete team '${team.name}'? All sharing with this team will also stop.`)))call('/api/rag/teams/delete',{name:team.name,confirmed:true},v=>L(`'${team.name}' 팀을 지웠어요. 이 팀에 준 공유 ${v.shares_removed}개도 거뒀어요.`,`Deleted team '${team.name}'. Also stopped ${v.shares_removed} shares with this team.`));}}>{L('팀 삭제','Delete team')}</button></div>
      <div className="rag-chips">{!team.members.length && <span className="footnote">{L('(사람 없음)','(no members)')}</span>}{team.members.map(m=><span className="rag-chip" key={m}><span aria-hidden="true">👤</span>{m}<button disabled={busy} aria-label={L(`${m}을(를) ${team.name} 팀에서 빼기`,`Remove ${m} from team ${team.name}`)} onClick={()=>{if(window.confirm(L(`${m}을(를) '${team.name}' 팀에서 뺄까요?`,`Remove ${m} from team '${team.name}'?`)))call('/api/rag/teams/remove',{team:team.name,member:m,confirmed:true},v=>v.removed?L(`${m}을(를) '${team.name}' 팀에서 뺐어요.`,`Removed ${m} from team '${team.name}'.`):L(`${m}을(를) 찾지 못했어요. 바뀐 것은 없어요.`,`Couldn’t find ${m}. Nothing changed.`));}}>✕</button></span>)}</div>
      <form className="rag-inline-form" onSubmit={async e=>{e.preventDefault();const value=(member[team.name]||'').trim();if(await call('/api/rag/teams/add',{team:team.name,member:value},v=>v.added?L(`${value}을(를) '${team.name}' 팀에 넣었어요.`,`Added ${value} to team '${team.name}'.`):L(`${value}을(를) 이미 넣어 두었어요.`,`${value} is already in the team.`)))setMember({...member,[team.name]:''});}}>
        <input className="rag-input" aria-label={L(`${team.name} 팀에 넣을 직원`,`Employee to add to team ${team.name}`)} list="rag-team-people" value={member[team.name]||''} disabled={busy} onChange={e=>setMember({...member,[team.name]:e.target.value})} placeholder={L('직원 이름(출입증 이름)','Employee name (token name)')}/><button disabled={busy || !(member[team.name]||'').trim()}>{L('사람 넣기','Add person')}</button>
      </form>
    </article>)}</div>
    {data.people.length>0 && <datalist id="rag-team-people">{data.people.map(x=><option key={x} value={x}/>)}</datalist>}
  </section>;
}

export function TokenSection({onChanged}){
  const [rows,setRows]=useState([]);
  const [name,setName]=useState('');
  const [issued,setIssued]=useState(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [message,setMessage]=useState('');
  async function refresh(){const value=await api('/api/rag/tokens');setRows(value.tokens);}
  useEffect(()=>{refresh().catch(e=>setError(e.message));},[]);
  async function issue(event){
    event.preventDefault();setBusy(true);setError('');setMessage('');setIssued(null);
    try{const value=await api('/api/rag/tokens/add',{name:name.trim()});setIssued(value);setName('');await refresh();onChanged?.();}
    catch(e){setError(e.message);}
    finally{setBusy(false);}
  }
  async function revoke(row){
    if(!window.confirm(L(`${row.name}의 출입증을 취소할까요? 그 토큰으로는 바로 회사 head를 쓸 수 없게 돼요.`,`Revoke ${row.name}’s token? It will stop working with the company head right away.`)))return;
    setBusy(true);setError('');setMessage('');
    try{await api('/api/rag/tokens/revoke',{name:row.name,confirmed:true});setMessage(L(`${row.name}의 출입증을 취소했어요.`,`Revoked ${row.name}’s token.`));if(issued?.name===row.name)setIssued(null);await refresh();onChanged?.();}
    catch(e){setError(e.message);}
    finally{setBusy(false);}
  }
  return <section className="manager-section rag-section"><h2>{L('직원 출입증 (토큰)','Employee tokens')}</h2>
    <p>{L('직원마다 출입증을 하나씩 만들어 전해 주세요. 직원은 내 문서 탭의 [회사에 연결]에 이 PC 주소와 출입증을 넣어요. 출입증 이름이 공유와 팀에서 쓰는 직원 이름이에요.','Create one token for each employee and give it to them. Employees enter this PC’s address and their token in [Connect to company] on the My documents tab. The token name is the employee name used for sharing and teams.')}</p>
    {error && <p role="alert" className="error">{error}</p>}{message && <p className="notice" role="status">{message}</p>}
    <form className="rag-inline-form" onSubmit={issue}><input className="rag-input" aria-label={L('출입증을 받을 직원 이름','Name of the employee getting the token')} value={name} disabled={busy} onChange={e=>setName(e.target.value)} placeholder={L('직원 이름 (예: kim)','Employee name (e.g. kim)')}/><button className="primary" disabled={busy || !name.trim()}>{L('출입증 만들기','Create token')}</button></form>
    {issued && <div className="rag-issued" role="status"><strong>{L(`${issued.name}의 출입증`,`${issued.name}’s token`)}</strong><code>{issued.token}</code><div className="row-actions"><CopyButton text={issued.token} label={L('출입증 복사','Copy token')}/><button onClick={()=>setIssued(null)}>{L('다 옮겨 적었어요','I’ve saved it')}</button></div><small className="warning">{L('다시 볼 수 없어요. 지금 복사해서 직원에게 안전하게 전해 주세요. 잃어버리면 취소하고 새로 만들어요.','You won’t be able to see this again. Copy it now and give it to the employee securely. If it’s lost, revoke it and create a new one.')}</small></div>}
    {!rows.length && <p className="footnote">{L('아직 만든 출입증이 없어요.','No tokens yet.')}</p>}
    <div className="connection-list">{rows.map(row=><article key={row.name}><div><h3>👤 {row.name}</h3><p>{L('만든 때','Created')} {new Date(row.created_at).toLocaleString(L('ko-KR','en-US'))}</p></div><div className="row-actions"><button className="danger" disabled={busy} onClick={()=>revoke(row)}>{L('출입증 취소','Revoke token')}</button></div></article>)}</div>
  </section>;
}
