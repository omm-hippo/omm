// Keep complete, recent turns within the local API's byte limit, including
// Korean text whose UTF-8 size differs from JavaScript string length.
export function sharedMessages(turns, question) {
  let messages=[{role:'user',content:question}];
  const encoder=new TextEncoder();
  for(const turn of turns.slice(-12).reverse()) {
    const candidate=[{role:'user',content:turn.user},{role:'assistant',content:turn.assistant},...messages];
    if(encoder.encode(JSON.stringify(candidate)).byteLength>45*1024) break;
    messages=candidate;
  }
  return messages;
}
