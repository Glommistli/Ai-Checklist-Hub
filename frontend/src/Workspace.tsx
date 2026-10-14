import {useEffect,useState,useRef} from 'react'
import {api} from './lib/api'
import type {Item,ParsedPlan} from './types'
import {X,RefreshCw,Undo2,ExternalLink,Check,FileText} from 'lucide-react'
import './workspace.css'

export const localDay=()=>new Date().toLocaleDateString('en-CA')
export const timeText=(value?:string|null)=>value?new Date(/(?:Z|[+-]\d{2}:\d{2})$/.test(value)?value:value+'Z').toLocaleString('zh-CN',{timeZoneName:'short'}):''
type Notify=(message:string,kind?:'success'|'error')=>void
type Pending={id:number;subject:string;sender:string;status:string;reason?:string;preview_title?:string;received_at?:string;confidence?:number;gmail_url:string;plan?:ParsedPlan;attempt_count:number}
type Job={status:string;stage:string;processed:number;total:number;subject?:string;error?:string}
type Usage={calls:number;input_tokens:number;output_tokens:number;daily_call_limit:number;recent:{id:number;model:string;status:string;elapsed_ms:number;created_at:string}[]}
type Controls={daily_call_limit:number;sender_allowlist:string[];sender_blocklist:string[]}
type Daily={capacity_minutes:number;estimated_minutes:number;over_capacity:boolean;planned_ids:number[];overdue_ids:number[];completed_ids:number[]}
type Review={completed:{id:number;title:string;evidence:string[];result:string}[];unfinished:{id:number;title:string;reason:string}[];markdown:string}
type Trash={id:number;kind:'item'|'list';title:string;deleted_at:string}
type Action={id:number;summary:string;kind:string;undone:boolean;created_at:string}
type Source={content:string;source_name?:string;excerpt?:string;gmail_url?:string;provider?:string;model?:string}
type Operation={kind:'add'|'update'|'merge'|'preserved';id?:number;title:string;ids?:number[];changes?:Record<string,{before:unknown;after:unknown}>;protected?:string[];protected_changes?:Record<string,{before:unknown;after:unknown}>;reason?:string}
type Proposal={id:number;operations:Operation[];provider?:string;model?:string}
const labels:Record<string,string>={partial:'部分失败',invalid:'输出无效',reading:'读取邮件',model:'模型分析',importing:'整理与入库',finished:'处理结束',failed:'处理失败',idle:'尚未处理',running:'进行中',paused:'已暂停',completed:'已完成',candidate:'待确认',processing:'中断或处理中',success:'成功',title:'标题',description:'说明',due_date:'截止日期',notes:'备注',estimated_minutes:'预计时长',acceptance_criteria:'验收要求'}
const errorText=(e:unknown)=>e instanceof Error?e.message:String(e)

export function WorkspacePage({view,items,onChanged,notify}:{view:string;items:Item[];onChanged:()=>Promise<void>;notify:Notify}){
 const [pending,setPending]=useState<Pending[]>([]),[job,setJob]=useState<Job>({status:'idle',stage:'idle',processed:0,total:0}),[usage,setUsage]=useState<Usage|null>(null)
 const [controls,setControls]=useState<Controls>({daily_call_limit:200,sender_allowlist:[],sender_blocklist:[]})
 const [daily,setDaily]=useState<Daily|null>(null),[day,setDay]=useState(localDay()),[capacity,setCapacity]=useState(240)
 const [review,setReview]=useState<Review|null>(null),[trash,setTrash]=useState<Trash[]>([]),[actions,setActions]=useState<Action[]>([])
 const [busy,setBusy]=useState(false),[error,setError]=useState('')
 const monday=()=>{const d=new Date(localDay()+'T12:00:00');d.setDate(d.getDate()-(d.getDay()+6)%7);return d.toLocaleDateString('en-CA')}
 const [week,setWeek]=useState(monday)
 const lastJob=useRef('idle')
 const loadPending=async()=>{const [p,j,u]=await Promise.all([api.get<Pending[]>('/api/workspace/pending'),api.get<Job>('/api/workspace/gmail-job'),api.get<Usage>('/api/workspace/usage')]);setPending(p);setJob(j);setUsage(u);if(lastJob.current==='running'&&j.status!=='running')await onChanged();lastJob.current=j.status}
 const load=async()=>{if(view==='pending'){await loadPending();setControls(await api.get<Controls>('/api/workspace/ai-controls'))}
  else if(view==='daily'){const d=await api.get<Daily>('/api/workspace/daily?day='+day);setDaily(d);setCapacity(d.capacity_minutes)}
  else if(view==='review')setReview(await api.get<Review>('/api/workspace/review?start='+week))
  else if(view==='trash')setTrash(await api.get<Trash[]>('/api/workspace/trash'))
  else if(view==='activity')setActions(await api.get<Action[]>('/api/workspace/actions'))}
 useEffect(()=>{setError('');void load().catch(e=>setError(errorText(e)))},[view,items,day,week])
 useEffect(()=>{if(view!=='pending'||job.status!=='running')return;const timer=window.setInterval(()=>{void loadPending().catch(e=>setError(errorText(e)))},1500);return()=>clearInterval(timer)},[view,job.status])
 const action=async(fn:()=>Promise<unknown>,message:string)=>{setBusy(true);setError('');try{await fn();await onChanged();await load();notify(message)}catch(e){setError(errorText(e));notify(errorText(e),'error')}finally{setBusy(false)}}
 const titles:Record<string,string>={pending:'待处理邮件',daily:'安排今日',review:'周复盘',trash:'回收站',activity:'操作记录'}
 const rootItems=items.filter(i=>!i.parent_id)
 return <div className="workspace-page"><div className="page-title"><div><p className="eyebrow">执行与回顾</p><h1>{titles[view]}</h1><p>{view==='pending'?'接收邮件并确认要加入清单的任务。':view==='daily'?'安排什么时候执行，不改变原来的截止日期。':view==='trash'?'删除的任务与清单可以恢复。':view==='activity'?'最近操作可以撤销；发生后续冲突时会保留最新修改。':'积累完成证据，回顾成果和未完成事项。'}</p></div></div>
 {error&&<div className="notice error">{error}</div>}
 {view==='pending'&&<>
  <section className="workspace-card"><div className="workspace-heading"><h2>邮件处理</h2><span className="workspace-badge">{labels[job.status]||job.status}</span></div><p>{labels[job.stage]||job.stage}{job.total? ` · ${job.processed}/${job.total} 封`:''}{job.subject?' · '+job.subject:''}</p>
   {job.total>0&&<progress max={Math.max(1,job.total)} value={job.processed}/>}
   {job.error&&<p className="workspace-error">{job.error}</p>}
   <div className="workspace-buttons">{job.status!=='paused'&&<button disabled={busy||job.status==='running'} className="primary" onClick={()=>action(()=>api.post('/api/workspace/gmail-job'),'已启动后台处理')}><RefreshCw/>{job.status==='running'?'正在处理…':'接收并处理邮件'}</button>}
   {job.status==='running'&&<button disabled={busy} className="secondary" onClick={()=>action(()=>api.post('/api/workspace/gmail-job/pause'),'当前请求结束后暂停')}>暂停</button>}
   {job.status==='paused'&&<button disabled={busy} className="secondary" onClick={()=>action(()=>api.post('/api/workspace/gmail-job/resume'),'已恢复处理')}>继续处理</button>}</div></section>
  <section className="workspace-card"><h2>待确认与失败邮件 · {pending.length}</h2>{!pending.length&&<p className="workspace-muted">没有需要处理的邮件。</p>}
   {pending.map(p=><article className="pending-row" key={p.id}><div><strong>{p.preview_title||p.subject||'尚未读取主题'}</strong><p className="workspace-muted">{p.sender} · {labels[p.status]||p.status} · 已尝试 {p.attempt_count} 次 · {timeText(p.received_at)}</p><p>{p.reason}</p>
   {p.plan&&<details><summary>查看模型任务</summary><ol>{p.plan.lists.flatMap(l=>l.items).map((i,index)=><li key={index}>{i.title}{i.subtasks.length>0&&<ul>{i.subtasks.map((child,n)=><li key={n}>{child.title}</li>)}</ul>}</li>)}</ol></details>}
   <div className="workspace-buttons"><a href={p.gmail_url} target="_blank" rel="noreferrer">原邮件 <ExternalLink size={14}/></a>
    {p.status==='candidate'&&<button className="primary" disabled={busy} onClick={()=>action(()=>api.post('/api/integrations/gmail/candidates/'+p.id+'/accept'),'已处理候选邮件')}>确认模型任务</button>}
    <button className="secondary" disabled={busy||job.status==='running'} onClick={()=>action(()=>api.post('/api/workspace/pending/'+p.id+'/retry'),'已重试邮件处理')}>{p.status==='processing'?'恢复处理':'重新分析'}</button>
    <button className="secondary" disabled={busy} onClick={()=>action(()=>api.post('/api/integrations/gmail/candidates/'+p.id+'/ignore'),'已忽略邮件')}>忽略</button></div></div></article>)}</section>
  <details className="workspace-card processing-rules"><summary>处理规则与模型用量</summary><h2>模型用量与发送者规则</h2>{usage&&<p>今日调用 {usage.calls}{usage.daily_call_limit?' / '+usage.daily_call_limit:''} 次 · 输入 {usage.input_tokens} tokens · 输出 {usage.output_tokens} tokens</p>}
   <div className="workspace-form"><label>每日调用上限（0 表示不限）<input type="number" min={0} max={10000} value={controls.daily_call_limit} onChange={e=>setControls({...controls,daily_call_limit:Number(e.target.value)})}/></label>
   <label>允许的发送者或域名（每行一个；留空接受全部）<textarea rows={2} value={controls.sender_allowlist.join('\n')} onChange={e=>setControls({...controls,sender_allowlist:e.target.value.split('\n')})}/></label>
   <label>排除的发送者或域名（每行一个）<textarea rows={2} value={controls.sender_blocklist.join('\n')} onChange={e=>setControls({...controls,sender_blocklist:e.target.value.split('\n')})}/></label></div>
   <button className="secondary" disabled={busy} onClick={()=>action(()=>api.put('/api/workspace/ai-controls',controls),'已保存处理规则')}>保存规则</button><p className="workspace-muted">规则用于之后处理的邮件；已入库任务不会重新扫描。未配置价格时仅展示调用与 token，不估算费用。</p>
   {usage&&<details><summary>最近模型请求</summary>{usage.recent.map(r=><p key={r.id}>{r.model} · {labels[r.status]||r.status} · {r.elapsed_ms} ms · {timeText(r.created_at)}</p>)}</details>}</details>
 </>}
 {view==='daily'&&<>
  <section className="workspace-card"><div className="workspace-form"><label>执行日期<input type="date" value={day} onChange={e=>setDay(e.target.value)}/></label><label>每日可用分钟<input type="number" min={15} max={1440} value={capacity} onChange={e=>setCapacity(Number(e.target.value))}/></label></div>
   <button className="secondary" onClick={()=>action(()=>api.put('/api/workspace/capacity',{minutes:capacity}),'已保存每日容量')} disabled={busy}>保存容量</button>
   {daily&&<p className={daily.over_capacity?'workspace-error':''}>已安排 {daily.estimated_minutes} / {daily.capacity_minutes} 分钟{daily.over_capacity?' · 超出容量，建议减少今日目标':''}</p>}</section>
  <section className="workspace-card"><h2>选择独立目标</h2>{rootItems.filter(i=>!i.completed).map(i=><label className="daily-choice" key={i.id}><input type="checkbox" disabled={busy} checked={daily?.planned_ids.includes(i.id)||false} onChange={e=>action(()=>api.put('/api/workspace/daily/'+i.id,{day:e.target.checked?day:null,exclude_day:e.target.checked?null:day}),'已调整执行安排')}/><span><strong>{i.title}</strong><small>{i.estimated_minutes?i.estimated_minutes+' 分钟':'未估时'}{i.due_date?' · 截止 '+i.due_date:''}{i.due_date&&i.due_date<localDay()?' · 已逾期':''}</small></span></label>)}</section>
 </>}
 {view==='trash'&&<section className="workspace-card">{!trash.length?<p>回收站为空。</p>:trash.map(row=><article className="workspace-row" key={row.kind+row.id}><div><strong>{row.title}</strong><p>{row.kind==='list'?'清单及其任务':'任务组'} · {timeText(row.deleted_at)}</p></div><button className="secondary" disabled={busy} onClick={()=>action(()=>api.post('/api/workspace/trash/'+row.kind+'/'+row.id+'/restore'),'已恢复')}>恢复</button></article>)}</section>}
 {view==='activity'&&<section className="workspace-card"><button className="secondary" disabled={busy} onClick={()=>action(()=>api.post('/api/workspace/undo'),'已撤销最近操作')}><Undo2/>撤销最近操作</button>{actions.map(a=><article className="workspace-row" key={a.id}><div><strong>{a.summary}</strong><p>{timeText(a.created_at)} · {a.undone?'已撤销':'已记录'}</p></div></article>)}</section>}
 {view==='review'&&<>
  <section className="workspace-card"><label>本周开始日期 <input type="date" value={week} onChange={e=>setWeek(e.target.value)}/></label><a className="secondary" href={'/api/workspace/review/export?start='+week}>导出 Markdown</a></section>
  {review&&<><section className="workspace-card"><h2>完成成果 · {review.completed.length}</h2>{review.completed.map(r=><article className="workspace-row" key={r.id}><div><strong>{r.title}</strong>{r.result&&<p>{r.result}</p>}{r.evidence.map(link=><p key={link}><a href={link} target="_blank" rel="noreferrer">{link}</a></p>)}</div><Check/></article>)}</section>
  <section className="workspace-card"><h2>未完成与下一步 · {review.unfinished.length}</h2>{review.unfinished.map(r=><article className="workspace-row" key={r.id}><div><strong>{r.title}</strong><p>{r.reason||'尚未记录延期原因或下一步'}</p></div></article>)}</section></>}
 </>}
 </div>
}

export function SourcePanel({item,close,onChanged,notify}:{item:Item;close:()=>void;onChanged:()=>Promise<void>;notify:Notify}){
 const [source,setSource]=useState<Source|null>(null),[proposal,setProposal]=useState<Proposal|null>(null),[selected,setSelected]=useState<number[]>([])
 const [overrides,setOverrides]=useState<Record<string,string[]>>({})
 const [busy,setBusy]=useState(false),[error,setError]=useState(''),[links,setLinks]=useState(Array.isArray(item.metadata.evidence)?item.metadata.evidence.map(String).join('\n'):'')
 const [result,setResult]=useState(String(item.metadata.result_note||'')),[delay,setDelay]=useState(String(item.metadata.delay_reason||''))
 useEffect(()=>{api.get<Source>('/api/workspace/source/'+item.id).then(setSource).catch(e=>setError(errorText(e)))},[item.id])
 const generate=async()=>{setBusy(true);setError('');try{const p=await api.post<Proposal>('/api/workspace/replan/'+item.id);setProposal(p);setOverrides({});setSelected(p.operations.flatMap((op,index)=>op.kind==='add'||(op.kind==='update'&&Object.keys(op.changes||{}).length)?[index]:[]))}catch(e){setError(errorText(e))}finally{setBusy(false)}}
 const apply=async()=>{if(!proposal)return;setBusy(true);setError('');try{const r=await api.post<{created:number;updated:number;merged:number}>('/api/workspace/replan/'+proposal.id+'/apply',{selected,overrides});await onChanged();notify(`已应用：新增 ${r.created}，修改 ${r.updated}，合并 ${r.merged}`);close()}catch(e){setError(errorText(e))}finally{setBusy(false)}}
 const save=async()=>{setBusy(true);setError('');try{await api.put('/api/workspace/evidence/'+item.id,{links:links.split('\n').map(s=>s.trim()).filter(Boolean),result_note:result,delay_reason:delay});await onChanged();notify('成果与下一步已保存')}catch(e){setError(errorText(e))}finally{setBusy(false)}}
 return <div className="modal-wrap"><section className="modal source-modal" role="dialog" aria-modal="true" aria-label="任务来源与成果"><div className="modal-head"><div><p className="eyebrow">任务来源与成果</p><h2>{item.title}</h2></div><button className="icon" aria-label="关闭来源" disabled={busy} onClick={close}><X/></button></div>
 {error&&<div className="notice error">{error}</div>}
 {source&&<section><p className="workspace-muted">{source.source_name||'手动任务'}{source.provider?' · '+source.provider:''}{source.model?' · '+source.model:''}</p>
 {source.gmail_url&&<a href={source.gmail_url} target="_blank" rel="noreferrer">查看原邮件 <ExternalLink size={14}/></a>}
 {source.excerpt&&<blockquote>{source.excerpt}</blockquote>}<details><summary>查看完整来源</summary><pre className="source-content">{source.content||'没有原始资料记录'}</pre></details></section>}
 <section className="workspace-card"><h3>成果与下一步</h3><label>成果链接（每行一个）<textarea rows={2} value={links} onChange={e=>setLinks(e.target.value)} placeholder="代码提交、文档或演示链接"/></label><label>一句结果<textarea rows={2} value={result} onChange={e=>setResult(e.target.value)}/></label><label>延期原因或下一步（可选）<textarea rows={2} value={delay} onChange={e=>setDelay(e.target.value)}/></label><button className="secondary" disabled={busy} onClick={save}>保存成果</button></section>
 <section className="workspace-card"><h3>AI 重新规划</h3><p className="workspace-muted">先比较差异，再选择应用。已完成任务和手动修改字段受到保护；合并不会自动选中。</p><p className="workspace-muted">生成建议会将完整来源和当前任务进度发送给你已启用的模型服务。</p><button className="secondary" disabled={busy||item.completed} onClick={generate}><RefreshCw/>{busy?'正在处理…':'生成规划提议'}</button>
 {proposal&&<><p>{proposal.provider} · {proposal.model}</p>{proposal.operations.map((op,index)=><article className="proposal-row" key={index}><label><input type="checkbox" disabled={busy||op.kind==='preserved'} checked={selected.includes(index)} onChange={e=>setSelected(e.target.checked?[...selected,index]:selected.filter(i=>i!==index))}/><strong>{op.kind==='add'?'新增':op.kind==='merge'?'合并':op.kind==='update'?'修改':'保留'}：{op.title}</strong></label>
 {op.ids&&<p>合并任务 ID：{op.ids.join('、')}</p>}{op.reason&&<p>{op.reason}</p>}{op.protected?.length? <p className="workspace-muted">保留已有字段：{op.protected.map(k=>labels[k]||k).join('、')}</p>:null}
 {Object.entries(op.protected_changes||{}).map(([key,value])=><div className="proposal-diff" key={'protected-'+key}><label><input type="checkbox" disabled={busy} checked={(overrides[String(index)]||[]).includes(key)} onChange={e=>{const fields=overrides[String(index)]||[];setOverrides({...overrides,[String(index)]:e.target.checked?[...fields,key]:fields.filter(f=>f!==key)});if(e.target.checked&&!selected.includes(index))setSelected([...selected,index])}}/>允许替换我的{labels[key]||key}</label><p><del>{String(value.before??'空')}</del></p><p>{String(value.after??'空')}</p></div>)}
 {Object.entries(op.changes||{}).map(([key,value])=><div className="proposal-diff" key={key}><small>{labels[key]||key}</small><p><del>{String(value.before??'空')}</del></p><p>{String(value.after??'空')}</p></div>)}</article>)}
 <button className="primary" disabled={busy||!selected.length} onClick={apply}>确认应用所选变更</button></>}
 </section></section></div>
}
