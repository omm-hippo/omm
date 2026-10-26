// Live step of a document import job, from the worker's stage + piece counts.
// Returns null once the job is finished so the final state renders as before.
export function documentProgress(job){
  if(!job || job.operation!=='document_import' || !['queued','running'].includes(job.status))return null;
  const total=Number.isInteger(job.total_count)?job.total_count:0;
  if(job.document_stage==='embed'){
    if(total<=0)return {label:'의미 색인 만드는 중',value:null,max:null};
    const done=Math.min(Math.max(job.completed_count||0,0),total);
    return {label:`의미 색인 만드는 중 ${done}/${total} 조각`,value:done,max:total};
  }
  if(job.document_stage==='upload')return {label:'회사 head로 보내고 색인 만드는 중',value:null,max:null};
  if(job.document_stage==='extract')return {label:'텍스트 추출 중',value:null,max:null};
  return {label:job.status==='queued'?'등록 대기 중':'문서 등록 준비 중',value:null,max:null};
}
