import React, {memo,useEffect,useRef,useState} from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {L} from './i18n.js';

export function CopyButton({text,label=L('복사','Copy')}) {
  const [status,setStatus]=useState('');
  const timer=useRef(null);
  useEffect(()=>()=>clearTimeout(timer.current),[]);
  async function copy() {
    clearTimeout(timer.current);
    try {await navigator.clipboard.writeText(text);setStatus(L('복사됨','Copied'));}
    catch {setStatus(L('복사하지 못했어요','Couldn\'t copy'));}
    timer.current=setTimeout(()=>setStatus(''),2200);
  }
  return <button type="button" className="copy-button" onClick={copy} aria-label={label}><span aria-live="polite">{status || label}</span></button>;
}

function CodeBlock({children}) {
  const child=React.Children.toArray(children).find(React.isValidElement);
  const source=String(child?.props.children ?? '').replace(/\n$/,'');
  const language=(child?.props.className || '').replace(/^language-/,'');
  return <div className="code-block"><div className="code-heading"><span>{language || L('코드','Code')}</span><CopyButton text={source} label={L('코드 복사','Copy code')}/></div><pre>{children}</pre></div>;
}
const components={
  pre:CodeBlock,
  table:({children})=><div className="markdown-table" tabIndex={0} role="region" aria-label={L('답변 표','Answer table')}><table>{children}</table></div>,
  a:({href,children})=><a href={href} target="_blank" rel="noreferrer noopener">{children}</a>,
  // Model output must not fetch remote images or inject raw HTML.
  img:({alt})=><span className="markdown-image">[{L('이미지','Image')}{alt?`: ${alt}`:''}]</span>,
};

export default memo(function RichText({text}) {
  return <div className="rich-text"><Markdown skipHtml remarkPlugins={[remarkGfm]} components={components}>{text}</Markdown></div>;
});
