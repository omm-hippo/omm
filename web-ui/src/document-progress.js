// Live step of a document import job, from the worker's stage + piece counts.
// Returns null once the job is finished so the final state renders as before.
import {L} from './i18n.js';

export function documentProgress(job){
  if(!job || job.operation!=='document_import' || !['queued','running'].includes(job.status))return null;
  const total=Number.isInteger(job.total_count)?job.total_count:0;
  if(job.document_stage==='embed'){
    if(total<=0)return {label:L('의미 색인 만드는 중','Building semantic index'),value:null,max:null};
    const done=Math.min(Math.max(job.completed_count||0,0),total);
    return {label:L(`의미 색인 만드는 중 ${done}/${total} 조각`,`Building semantic index ${done}/${total} chunks`),value:done,max:total};
  }
  if(job.document_stage==='upload')return {label:L('회사 head로 보내고 색인 만드는 중','Sending to company head and indexing'),value:null,max:null};
  if(job.document_stage==='extract')return {label:L('텍스트 추출 중','Extracting text'),value:null,max:null};
  return {label:job.status==='queued'?L('등록 대기 중','Waiting to import'):L('문서 등록 준비 중','Preparing import'),value:null,max:null};
}
