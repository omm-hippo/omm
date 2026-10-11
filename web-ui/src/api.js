export async function api(path, body, signal) {
  let response;
  try { response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    credentials: 'same-origin',
    headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-OMM-Web': '1'},
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: signal ? AbortSignal.any([signal,AbortSignal.timeout(90000)]) : AbortSignal.timeout(path === '/api/wiki/discover' ? 90000 : path === '/api/diagnostics' ? 60000 : path === '/api/catalog/refresh' ? 40000 : 15000)
  }); } catch {
    throw new Error('서버와 연결하지 못했어요. 작업이 접수됐을 수 있으니 작업 목록을 먼저 확인해 주세요.');
  }
  let value;
  try { value = await response.json(); } catch { throw new Error('서버 응답을 확인할 수 없어요. 작업 목록을 먼저 확인해 주세요.'); }
  if (!response.ok) throw new Error(value.error || '요청을 처리하지 못했어요.');
  return value;
}

export const gib = value => typeof value === 'number' ? `${value.toFixed(1)} GB` : '정보 없음';
export function bytes(value) {
  if(typeof value !== 'number' || !Number.isFinite(value) || value < 0)return '크기 확인 필요';
  const units=['B','KiB','MiB','GiB','TiB'];
  const index=value>0 ? Math.min(units.length-1,Math.floor(Math.log(value)/Math.log(1024))) : 0;
  return `${(value/1024**index).toFixed(index?1:0)} ${units[index]}`;
}
