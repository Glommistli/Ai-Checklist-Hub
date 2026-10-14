import {useEffect,useState} from 'react'
import {Archive,CalendarDays,Check,ChevronDown,FileUp,Mail,Plus,Settings,X} from 'lucide-react'
import {api} from './lib/api'
import type {List,View} from './types'

export const taskViews:View[]=['today','inbox','upcoming','all','daily']
export const recordViews:View[]=['review','history','activity','trash']
export function ViewTabs({view,setView,kind}:{view:View;setView:(view:View)=>void;kind:'tasks'|'records'}){
 const tabs:[View,string][]=kind==='tasks'?[['today','今天'],['inbox','收集箱'],['upcoming','即将到来'],['all','全部任务']]:[['review','周复盘'],['history','导入历史'],['activity','操作记录'],['trash','回收站']]
 return <nav className="view-tabs" aria-label={kind==='tasks'?'任务视图':'记录与回顾'}>{tabs.map(([id,label])=><button key={id} className={view===id?'selected':''} aria-current={view===id?'page':undefined} onClick={()=>setView(id)}>{label}</button>)}</nav>
}

export function Sidebar({open,close,view,setView,lists,addList,pendingCount}:{open:boolean;close:()=>void;view:View;setView:(v:View)=>void;lists:List[];addList:()=>void;pendingCount:number}){
 const [completedOpen,setCompletedOpen]=useState(false)
 const resizeTo=(width:number)=>{const next=Math.max(220,Math.min(360,Math.round(width)));document.documentElement.style.setProperty('--sidebar-width',`${next}px`);localStorage.sidebarWidth=String(next);return next}
 const persistWidth=(width:number)=>{void api.put('/api/settings/ui',{sidebar_width:resizeTo(width)}).catch(()=>{})}
 useEffect(()=>{const saved=Number(localStorage.sidebarWidth);if(Number.isFinite(saved)&&saved>0)resizeTo(saved);void api.get<{sidebar_width:number}>('/api/settings/ui').then(settings=>resizeTo(settings.sidebar_width)).catch(()=>{})},[])
 const currentWidth=()=>Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-width'))||256
 const startResize=(event:React.PointerEvent<HTMLDivElement>)=>{if(window.innerWidth<=760)return;event.preventDefault();const startX=event.clientX,startWidth=currentWidth();document.body.classList.add('sidebar-resizing');const move=(e:PointerEvent)=>resizeTo(startWidth+e.clientX-startX);const stop=()=>{persistWidth(currentWidth());document.body.classList.remove('sidebar-resizing');window.removeEventListener('pointermove',move);window.removeEventListener('pointerup',stop);window.removeEventListener('pointercancel',stop)};window.addEventListener('pointermove',move);window.addEventListener('pointerup',stop);window.addEventListener('pointercancel',stop)}
 const isCompleted=(list:List)=>list.item_count>0&&list.pending_count===0
 const userLists=lists.filter(list=>list.name!=='Inbox'),activeLists=userLists.filter(list=>!isCompleted(list)),completedLists=userLists.filter(isCompleted)
 const renderList=(list:List)=><button key={list.id} title={list.name} className={`${view===`list-${list.id}`?'active ':''}${isCompleted(list)?'completed-list':''}`} aria-current={view===`list-${list.id}`?'page':undefined} onClick={()=>setView(`list-${list.id}`)}><i style={{background:list.color||'#858581'}}/><span>{list.name}</span>{isCompleted(list)?<Check className="list-complete-mark" aria-label="已全部完成"/>:<em>{list.pending_count}</em>}</button>
 const selectedCompleted=completedLists.find(list=>view===`list-${list.id}`)
 return <><aside className={`sidebar ${open?'open':''}`} aria-label="主导航">
  <div className="brand"><span className="brandmark"><Check/></span><span>Checklist</span><button className="icon mobile" aria-label="关闭侧栏" onClick={close}><X/></button></div>
  <nav className="primary-nav" aria-label="工作区">
   <button className={taskViews.includes(view)?'active':''} onClick={()=>setView('today')}><CalendarDays/><span>任务概览</span></button>
   <button className={view==='pending'?'active':''} onClick={()=>setView('pending')}><Mail/><span>待处理邮件</span>{pendingCount>0&&<em>{pendingCount}</em>}</button>
  </nav>
  <section className="checklist-area"><div className="section-head"><span>我的清单</span><button onClick={addList} aria-label="新建清单" title="新建清单"><Plus/></button></div>
   <div className="checklist-scroll"><nav className="list-nav" aria-label="未完成清单">{activeLists.map(renderList)}</nav>{!userLists.length&&<p className="sidebar-empty">点击 + 创建第一份清单</p>}
    {completedLists.length>0&&<div className="completed-section"><button className="completed-toggle" aria-expanded={completedOpen} aria-controls="completed-lists" onClick={()=>setCompletedOpen(!completedOpen)}><ChevronDown className={completedOpen?'expanded':''}/><span>已完成清单</span><em>{completedLists.length}</em></button>{completedOpen?<nav id="completed-lists" className="list-nav" aria-label="已完成清单">{completedLists.map(renderList)}</nav>:selectedCompleted&&<nav className="list-nav" aria-label="当前已完成清单">{renderList(selectedCompleted)}</nav>}</div>}
   </div>
  </section>
  <nav className="sidebar-bottom" aria-label="管理">
   <button className={view==='import'?'active':''} onClick={()=>setView('import')}><FileUp/><span>导入与规划</span></button>
   <button className={recordViews.includes(view)?'active':''} onClick={()=>setView('review')}><Archive/><span>记录与回顾</span></button>
   <button className={view==='settings'?'active':''} onClick={()=>setView('settings')}><Settings/><span>设置</span></button>
  </nav>
  <div className="sidebar-resizer" role="separator" aria-label="调整侧边栏宽度" aria-orientation="vertical" tabIndex={0} onPointerDown={startResize} onKeyDown={event=>{if(event.key==='ArrowLeft'||event.key==='ArrowRight'){event.preventDefault();persistWidth(currentWidth()+(event.key==='ArrowRight'?12:-12))}}} onDoubleClick={()=>persistWidth(256)}/>
 </aside>{open&&<button className="scrim" aria-label="关闭侧栏" onClick={close}/>}</>
}
