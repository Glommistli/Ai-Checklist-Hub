import type {ReactNode} from 'react'
import {Mail} from 'lucide-react'

export type GmailStatus={authorization_required?:boolean;client_configured:boolean;connected:boolean;email?:string|null;history_id?:string|null;last_synced_at?:string|null;last_sync_error?:string|null;sync_interval_hours:number;lookback_days:number;auto_import:boolean;auto_import_threshold:number;exclude_promotions:boolean;candidate_count:number;processed_count:number}

export function GmailConnectionCard({status,children}:{status:GmailStatus|null;children?:ReactNode}){
 const label=status?.connected?`已连接 ${status.email||''}`:status?.authorization_required?'授权已失效，请重新连接':status?'未连接':'正在读取连接状态…'
 return <section className="settings-card gmail-card">
  <div className="gmail-heading"><h2><Mail/>Gmail 连接</h2><span className={`integration-state ${status?.connected?'connected':''}`} role="status">{label}</span></div>
  <p>邮件接收、处理进度和候选任务确认请前往“待处理邮件”。</p>
  {status&&!status.connected&&children}
 </section>
}
