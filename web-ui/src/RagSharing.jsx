import React, {useEffect, useRef, useState} from 'react';
import {api} from './api';
import {CopyButton} from './RichText';

// Sharing pieces of the 내 문서 tab. The employee token never reaches this
// page: the local server keeps it and answers only with the head address and
// the token name. A newly issued token is the one exception, shown once.

export function LocationBar({rag,onConnect,onLocal,busy}){
  const remote=rag?.mode==='remote';
  return <section className="rag-location" aria-label="지금 보는 곳">
    <div><span className="rag-location-label">📍 지금 보는 곳</span>
      <strong>{!rag?'확인 중…':remote?`회사 head${rag.remote?.url?` (${rag.remote.url})`:''}`:'이 PC'}</strong>
      {remote && rag.remote?.name && <small>나: {rag.remote.name}</small>}
      {!remote && rag && <small>이 PC의 문서 저장소 · 관리자</small>}
    </div>
    {rag && <div className="row-actions"><button disabled={busy} onClick={onConnect}>{remote?'다른 출입증으로 연결':'회사에 연결'}</button>{remote && <button disabled={busy} onClick={onLocal}>이 PC로 돌아가기</button>}</div>}
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
    <h2>회사에 연결</h2>
    <p>관리자에게 받은 회사 head 주소와 직원 출입증(토큰)을 넣어 주세요. 연결하기 전에 head에 토큰을 보여 주고 누구인지 확인해요.</p>
    {error && <p role="alert" className="error">{error}</p>}
    <label>회사 head 주소<input className="rag-input" value={url} disabled={busy} onChange={e=>setUrl(e.target.value)} placeholder="예: 192.168.0.10 또는 http://head.local:52440" autoComplete="off" required/></label>
    <label>직원 출입증(토큰)<input className="rag-input" type="password" value={token} disabled={busy} onChange={e=>setToken(e.target.value)} placeholder="omm_..." autoComplete="off" required/></label>
    <p className="footnote">토큰은 이 PC의 OMM 폴더(rag-remote.json)에만 저장하고 화면에 다시 보여 주지 않아요.</p>
    <div className="dialog-actions"><button type="button" disabled={busy} onClick={onClose}>취소</button><button className="primary" disabled={busy || !url.trim() || !token.trim()}>{busy?'확인 중…':'확인하고 연결'}</button></div>
  </form></dialog>;
}

function AddTarget({kind,options,onAdd,busy}){
  const [open,setOpen]=useState(false);
  const [value,setValue]=useState('');
  const list=`rag-${kind}-options`;
  if(!open)return <button disabled={busy} onClick={()=>setOpen(true)}>{kind==='person'?'사람 추가':'팀 추가'}</button>;
  return <form className="rag-inline-form" onSubmit={async e=>{e.preventDefault();if(await onAdd(kind,value.trim())){setValue('');setOpen(false);}}}>
    <input className="rag-input" aria-label={kind==='person'?'직원 이름(토큰 이름)':'팀 이름'} list={options?.length?list:undefined} value={value} disabled={busy} onChange={e=>setValue(e.target.value)} placeholder={kind==='person'?'직원 이름(토큰 이름)':'팀 이름'} autoFocus/>
    {options?.length>0 && <datalist id={list}>{options.map(x=><option key={x} value={x}/>)}</datalist>}
    <button className="primary" disabled={busy || !value.trim()}>공유</button><button type="button" disabled={busy} onClick={()=>setOpen(false)}>취소</button>
  </form>;
}

export function SharePanel({collection,people,teams,busy,onChange,onError}){
  const shares=collection.shares || [];
  async function add(kind,target){
    try{const value=await api('/api/rag/share',{collection:collection.name,kind,target});onChange(value.added?`${kind==='team'?'팀 ':''}${target}에게 공유했어요. 이제 검색할 수 있어요.`:`이미 ${kind==='team'?'팀 ':''}${target}에게 공유돼 있어요.`);return true;}
    catch(e){onError(e.message);return false;}
  }
  async function remove(share){
    const label=`${share.kind==='team'?'팀 ':''}${share.target}`;
    if(!window.confirm(`'${collection.name}' 컬렉션 공유를 ${label}에게서 거둘까요? 바로 검색할 수 없게 돼요.`))return;
    try{const value=await api('/api/rag/unshare',{collection:collection.name,kind:share.kind,target:share.target,confirmed:true});onChange(value.removed?`${label}에게서 공유를 거뒀어요.`:`${label}에게 공유돼 있지 않았어요. 바뀐 것은 없어요.`);}
    catch(e){onError(e.message);}
  }
  return <section className="rag-share"><h3>공유</h3>
    <p className="footnote">만든 사람과 관리자는 늘 검색할 수 있어요. 여기서 정한 사람·팀만 더 볼 수 있어요.</p>
    <div className="rag-chips">{!shares.length && <span className="footnote">아직 아무에게도 공유하지 않았어요.</span>}{shares.map(s=><span className="rag-chip" key={s.kind+s.target}><span aria-hidden="true">{s.kind==='team'?'👥':'👤'}</span>{s.kind==='team'?'팀 ':''}{s.target}<button disabled={busy} aria-label={`${s.target} 공유 거두기`} onClick={()=>remove(s)}>✕</button></span>)}</div>
    <div className="row-actions rag-share-actions"><AddTarget kind="person" options={people} onAdd={add} busy={busy}/><AddTarget kind="team" options={teams} onAdd={add} busy={busy}/></div>
  </section>;
}

const CONNECTION_STATE={added:'연결했어요.',updated:'연결 설정을 새로 고쳤어요.',unchanged:'이미 연결돼 있어요.'};
const DISCONNECT_STATE={restored:'원래 설정으로 되돌렸어요.',removed:'OMM 항목만 지우고 다른 설정은 그대로 두었어요.',absent:'OMM 항목이 이미 없었어요. 바뀐 것은 없어요.',not_connected:'OMM이 연결한 적이 없어요. 바뀐 것은 없어요.'};

export function AgentSection({rag,onRefresh}){
  const [busy,setBusy]=useState('');
  const [message,setMessage]=useState('');
  const [error,setError]=useState('');
  const remote=rag?.mode==='remote';
  async function act(row,removing){
    if(removing && !window.confirm(`${row.label}에서 OMM 문서 검색 연결을 해제할까요? 다른 설정은 그대로 둬요.`))return;
    setBusy(row.harness);setError('');setMessage('');
    try{
      const value=await api(removing?'/api/rag/disconnect':'/api/rag/connect',removing?{harness:row.harness,confirmed:true}:{harness:row.harness});
      setMessage(removing?`${row.label}: ${DISCONNECT_STATE[value.state]||'연결을 해제했어요.'}`:`${row.label}: ${CONNECTION_STATE[value.state]||'연결했어요.'} ${row.label}을(를) 다시 시작해야 문서 검색 도구가 보여요.${row.harness==='claude-code'?' Claude Code가 켜져 있으면 완전히 끄고 다시 켜 주세요.':''}${value.remote?' 직원 토큰이 그 도구의 설정 파일에 저장돼요. 파일을 공유하거나 올리지 마세요.':''}`);
      await onRefresh();
    }catch(e){setError(e.message);}
    finally{setBusy('');}
  }
  return <section className="manager-section rag-section"><h2>AI 연결</h2>
    <p>코딩 에이전트가 {remote?'회사 head에서 나에게 공유된 문서':'이 PC의 문서'}를 근거로 찾을 수 있게 연결해요. 원문 인용과 쪽 번호를 함께 받아요.</p>
    {error && <p role="alert" className="error">{error}</p>}{message && <p className="notice" role="status">{message}</p>}
    <div className="connection-list">{(rag?.harnesses||[]).map(row=>{
      const where=row.registered?(row.remote?'회사 head 문서':'이 PC 문서'):null;
      return <article key={row.harness}><div><h3>{row.label}</h3>
        <p>{row.error?row.error:!row.installed && !row.registered?'이 PC에서 찾지 못했어요(설치되지 않음)':row.registered?`연결됨 · ${where}`:'연결 안 됨'}</p>
        {row.registered && where && ((row.remote && !remote) || (!row.remote && remote)) && <small className="warning">지금 보는 곳과 다른 곳에 연결돼 있어요. 다시 [연결]하면 지금 보는 곳으로 바꿔요.</small>}
        {row.registered && !row.managed && <small>OMM이 만든 항목이 아니에요. 직접 넣은 설정은 건드리지 않아요.</small>}
      </div><div className="row-actions">
        {(row.installed || row.registered) && !row.error && (!row.registered || (row.managed && ((row.remote && !remote) || (!row.remote && remote) || (!row.remote && !row.current)))) && <button className="primary" disabled={!!busy} onClick={()=>act(row,false)}>{busy===row.harness?'연결 중…':'연결'}</button>}
        {row.registered && row.managed && <button disabled={!!busy} onClick={()=>act(row,true)}>{busy===row.harness?'해제 중…':'해제'}</button>}
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
  return <section className="manager-section rag-section"><h2>팀 관리</h2>
    <p>팀을 만들어 직원(출입증 이름)을 넣으면 컬렉션을 팀 단위로 공유할 수 있어요. 팀은 이 PC(관리자)에서만 관리해요.</p>
    {error && <p role="alert" className="error">{error}</p>}{message && <p className="notice" role="status">{message}</p>}
    <form className="rag-inline-form" onSubmit={async e=>{e.preventDefault();if(await call('/api/rag/teams/create',{name:name.trim()},()=>`'${name.trim()}' 팀을 만들었어요. 직원을 넣어 주세요.`))setName('');}}>
      <input className="rag-input" aria-label="새 팀 이름" value={name} disabled={busy} onChange={e=>setName(e.target.value)} placeholder="새 팀 이름 (예: sales)"/><button className="primary" disabled={busy || !name.trim()}>팀 만들기</button>
    </form>
    {!data.teams.length && <p className="footnote">아직 팀이 없어요.</p>}
    <div className="rag-team-list">{data.teams.map(team=><article key={team.name}>
      <div className="rag-team-heading"><h3>👥 {team.name}</h3><button className="danger" disabled={busy} onClick={()=>{if(window.confirm(`'${team.name}' 팀을 지울까요? 이 팀에 준 공유도 모두 거둬요.`))call('/api/rag/teams/delete',{name:team.name,confirmed:true},v=>`'${team.name}' 팀을 지웠어요. 이 팀에 준 공유 ${v.shares_removed}개도 거뒀어요.`);}}>팀 삭제</button></div>
      <div className="rag-chips">{!team.members.length && <span className="footnote">(사람 없음)</span>}{team.members.map(m=><span className="rag-chip" key={m}><span aria-hidden="true">👤</span>{m}<button disabled={busy} aria-label={`${m}을(를) ${team.name} 팀에서 빼기`} onClick={()=>{if(window.confirm(`${m}을(를) '${team.name}' 팀에서 뺄까요?`))call('/api/rag/teams/remove',{team:team.name,member:m,confirmed:true},v=>v.removed?`${m}을(를) '${team.name}' 팀에서 뺐어요.`:`${m}을(를) 찾지 못했어요. 바뀐 것은 없어요.`);}}>✕</button></span>)}</div>
      <form className="rag-inline-form" onSubmit={async e=>{e.preventDefault();const value=(member[team.name]||'').trim();if(await call('/api/rag/teams/add',{team:team.name,member:value},v=>v.added?`${value}을(를) '${team.name}' 팀에 넣었어요.`:`${value}을(를) 이미 넣어 두었어요.`))setMember({...member,[team.name]:''});}}>
        <input className="rag-input" aria-label={`${team.name} 팀에 넣을 직원`} list="rag-team-people" value={member[team.name]||''} disabled={busy} onChange={e=>setMember({...member,[team.name]:e.target.value})} placeholder="직원 이름(출입증 이름)"/><button disabled={busy || !(member[team.name]||'').trim()}>사람 넣기</button>
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
    if(!window.confirm(`${row.name}의 출입증을 취소할까요? 그 토큰으로는 바로 회사 head를 쓸 수 없게 돼요.`))return;
    setBusy(true);setError('');setMessage('');
    try{await api('/api/rag/tokens/revoke',{name:row.name,confirmed:true});setMessage(`${row.name}의 출입증을 취소했어요.`);if(issued?.name===row.name)setIssued(null);await refresh();onChanged?.();}
    catch(e){setError(e.message);}
    finally{setBusy(false);}
  }
  return <section className="manager-section rag-section"><h2>직원 출입증 (토큰)</h2>
    <p>직원마다 출입증을 하나씩 만들어 전해 주세요. 직원은 내 문서 탭의 [회사에 연결]에 이 PC 주소와 출입증을 넣어요. 출입증 이름이 공유와 팀에서 쓰는 직원 이름이에요.</p>
    {error && <p role="alert" className="error">{error}</p>}{message && <p className="notice" role="status">{message}</p>}
    <form className="rag-inline-form" onSubmit={issue}><input className="rag-input" aria-label="출입증을 받을 직원 이름" value={name} disabled={busy} onChange={e=>setName(e.target.value)} placeholder="직원 이름 (예: kim)"/><button className="primary" disabled={busy || !name.trim()}>출입증 만들기</button></form>
    {issued && <div className="rag-issued" role="status"><strong>{issued.name}의 출입증</strong><code>{issued.token}</code><div className="row-actions"><CopyButton text={issued.token} label="출입증 복사"/><button onClick={()=>setIssued(null)}>다 옮겨 적었어요</button></div><small className="warning">다시 볼 수 없어요. 지금 복사해서 직원에게 안전하게 전해 주세요. 잃어버리면 취소하고 새로 만들어요.</small></div>}
    {!rows.length && <p className="footnote">아직 만든 출입증이 없어요.</p>}
    <div className="connection-list">{rows.map(row=><article key={row.name}><div><h3>👤 {row.name}</h3><p>만든 때 {new Date(row.created_at).toLocaleString('ko-KR')}</p></div><div className="row-actions"><button className="danger" disabled={busy} onClick={()=>revoke(row)}>출입증 취소</button></div></article>)}</div>
  </section>;
}
