import React, {useEffect,useRef,useState} from 'react';
import {destinations,searchDestinations} from './workspace-state.js';

export function WorkspaceIcon({name,size=18}) {
  const paths={
    wiki:<><path d="M3 4h6l3 2 3-2h6v15h-6l-3 2-3-2H3Z"/><path d="M12 6v15"/></>,
    recommend:<><path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z"/></>,
    models:<><path d="m12 3 9 5-9 5-9-5ZM3 8v10l9 5 9-5V8M12 13v10"/></>,
    chat:<path d="M21 14a4 4 0 0 1-4 4H8l-5 4V6a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4Z"/>,
    documents:<><path d="M14 2H5v20h14V7ZM14 2v5h5M8 12h8M8 16h6"/></>,
    comparison:<><path d="M4 3v18h17M9 17v-6M14 17V7M19 17V4"/></>,
    jobs:<><path d="M9 6h12M9 12h12M9 18h12M3 6h1M3 12h1M3 18h1"/></>,
    connections:<><path d="M8 3v6M16 3v6M6 9h12v2a6 6 0 0 1-12 0ZM12 17v5"/></>,
    diagnostics:<><path d="M3 12h4l3-8 4 16 3-8h4"/></>,
    settings:<><path d="M4 6h16M4 12h16M4 18h16"/><circle cx="8" cy="6" r="2"/><circle cx="16" cy="12" r="2"/><circle cx="10" cy="18" r="2"/></>,
    search:<><circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/></>,
    menu:<><path d="M4 6h16M4 12h16M4 18h16"/></>,
    close:<path d="m6 6 12 12M6 18 18 6"/>,
    refresh:<><path d="M20 8a8 8 0 1 0 0 8M20 3v5h-5"/></>,
  };
  return <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>;
}

export function Sidebar({tab,onNavigate,onSearch,appearance,onAppearance,activeJobs}) {
  return <>
    <a className="brand" href="#wiki" onClick={e=>{e.preventDefault();onNavigate('wiki');}}>omm<span>로컬 모델 작업 공간</span></a>
    <button className="workspace-search" onClick={onSearch}><WorkspaceIcon name="search"/><span>메뉴 검색</span><kbd>⌘/Ctrl K</kbd></button>
    <nav aria-label="주 메뉴">{destinations.filter(x=>!x.secondary).map(item=><button aria-current={tab===item.id?'page':undefined} className={tab===item.id?'selected':''} key={item.id} onClick={()=>onNavigate(item.id)}><WorkspaceIcon name={item.icon}/><span>{item.label}</span>{item.id==='jobs' && activeJobs>0 && <span className="nav-count">{activeJobs}</span>}</button>)}</nav>
    <nav className="secondary-nav" aria-label="관리 메뉴">{destinations.filter(x=>x.secondary).map(item=><button aria-current={tab===item.id?'page':undefined} className={tab===item.id?'selected':''} key={item.id} onClick={()=>onNavigate(item.id)}><WorkspaceIcon name={item.icon}/>{item.label}</button>)}</nav>
    <div className="workspace-footer"><label htmlFor="appearance">화면 모드</label><select id="appearance" value={appearance} onChange={e=>onAppearance(e.target.value)}><option value="system">시스템 설정</option><option value="light">밝게</option><option value="dark">어둡게</option></select><p className="local-note"><span/>이 컴퓨터에서 실행 중</p></div>
  </>;
}

export function CommandPalette({onClose,onNavigate}) {
  const dialog=useRef(null),input=useRef(null);
  const [query,setQuery]=useState('');
  const results=searchDestinations(query);
  useEffect(()=>{dialog.current.showModal();input.current.focus();},[]);
  function select(id){onNavigate(id);onClose();}
  return <dialog className="command-palette" ref={dialog} aria-labelledby="command-title" onCancel={onClose} onClick={e=>{if(e.target===dialog.current)onClose();}}>
    <h2 id="command-title" className="sr-only">메뉴 검색</h2>
    <div className="command-search"><WorkspaceIcon name="search"/><input ref={input} value={query} maxLength={100} placeholder="어디로 이동할까요?" aria-label="메뉴 검색어" onChange={e=>setQuery(e.target.value)} onKeyDown={e=>{if(e.key==='Enter' && !e.nativeEvent.isComposing && e.keyCode!==229 && results.length){e.preventDefault();select(results[0].id);}}}/><button className="icon-button" aria-label="메뉴 검색 닫기" onClick={onClose}><WorkspaceIcon name="close"/></button></div>
    <div className="command-results">{results.length?results.map(item=><button key={item.id} onClick={()=>select(item.id)}><WorkspaceIcon name={item.icon}/><span><strong>{item.label}</strong><small>{item.detail}</small></span></button>):<p role="status">일치하는 메뉴가 없어요.</p>}</div><p className="command-hint">Enter 첫 결과 열기 · Tab 결과 이동 · Esc 닫기</p>
  </dialog>;
}
