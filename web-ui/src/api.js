export async function api(path, body) {
  let response;
  try { response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    credentials: 'same-origin',
    headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-OMM-Web': '1'},
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(path === '/api/catalog/refresh' ? 40000 : 15000)
  }); } catch {
    throw new Error('서버와 연결하지 못했어요. 작업이 접수됐을 수 있으니 작업 목록을 먼저 확인해 주세요.');
  }
  let value;
  try { value = await response.json(); } catch { throw new Error('서버 응답을 확인할 수 없어요. 작업 목록을 먼저 확인해 주세요.'); }
  if (!response.ok) throw new Error(value.error || '요청을 처리하지 못했어요.');
  return value;
}

export const gib = value => typeof value === 'number' ? `${value.toFixed(1)} GB` : '정보 없음';
export const bytes = value => value > 0 ? `${(value / 1048576).toFixed(1)} MiB` : '0 MiB';
