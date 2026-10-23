import React, {memo,useEffect,useRef,useState} from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

export function CopyButton({text,label='복사'}) {
  const [status,setStatus]=useState('');
  const timer=useRef(null);
  useEffect(()=>()=>clearTimeout(timer.current),[]);
  async function copy() {
    clearTimeout(timer.current);
    try {await navigator.clipboard.writeText(text);setStatus('복사됨');}
    catch {setStatus('복사하지 못했어요');}
    timer.current=setTimeout(()=>setStatus(''),2200);
  }
  return <button type="button" className="copy-button" onClick={copy} aria-label={label}><span aria-live="polite">{status || label}</span></button>;
}

function CodeBlock({children}) {
  const child=React.Children.toArray(children).find(React.isValidElement);
  const source=String(child?.props.children ?? '').replace(/\n$/,'');
  const language=(child?.props.className || '').replace(/^language-/,'');
  return <div className="code-block"><div className="code-heading"><span>{language || '코드'}</span><CopyButton text={source} label="코드 복사"/></div><pre>{children}</pre></div>;
}
const components={
  pre:CodeBlock,
  table:({children})=><div className="markdown-table" tabIndex={0} role="region" aria-label="답변 표"><table>{children}</table></div>,
  a:({href,children})=><a href={href} target="_blank" rel="noreferrer noopener">{children}</a>,
  // Model output must not fetch remote images or inject raw HTML.
  img:({alt})=><span className="markdown-image">[이미지{alt?`: ${alt}`:''}]</span>,
};

export default memo(function RichText({text}) {
  return <div className="rich-text"><Markdown skipHtml remarkPlugins={[remarkGfm]} components={components}>{text}</Markdown></div>;
});
