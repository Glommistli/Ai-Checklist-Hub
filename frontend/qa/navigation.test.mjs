import assert from 'node:assert/strict'
import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {createRequire,Module} from 'node:module'
import {fileURLToPath} from 'node:url'
import {build} from 'vite'
import react from '@vitejs/plugin-react'

const require=createRequire(import.meta.url)
const root=fileURLToPath(new URL('../',import.meta.url))
async function loadComponent(name) {
 const result=await build({root,configFile:false,plugins:[react()],logLevel:'silent',build:{ssr:root+'src/'+name+'.tsx',write:false,rollupOptions:{output:{format:'cjs'}}}})
 const entry=result.output.find(file=>file.type==='chunk'&&file.isEntry)
 const module=new Module(root+'qa/'+name+'.cjs')
 module.require=require
 module._compile(entry.code,module.id)
 return module.exports
}
{
 const {Sidebar,ViewTabs}=await loadComponent('Navigation')
 const {GmailConnectionCard}=await loadComponent('GmailConnectionCard')
 const lists=[
  {id:1,name:'Inbox',item_count:0,pending_count:0},
  {id:2,name:'已结束的学习清单',item_count:5,pending_count:0},
  {id:3,name:'进行中的学习清单',item_count:5,pending_count:2},
  {id:4,name:'空清单',item_count:0,pending_count:0},
 ]
 const sidebar=(view='today',data=lists)=>renderToStaticMarkup(React.createElement(Sidebar,{open:false,close(){},view,setView(){},lists:data,addList(){},pendingCount:3}))
 const ordinary=sidebar()
 assert.ok(ordinary.includes('进行中的学习清单')&&ordinary.includes('空清单'),'Active and empty lists remain accessible')
 assert.ok(!ordinary.includes('已结束的学习清单'),'Completed lists are collapsed by default')
 assert.ok(!ordinary.includes('>Inbox<'),'Inbox belongs to task views, not personal lists')
 assert.ok(!ordinary.includes('更多视图')&&!ordinary.includes('>工具<'),'No duplicate expandable utility hierarchy')
 const selected=sidebar('list-2')
 assert.ok(selected.includes('completed-list')&&selected.includes('已结束的学习清单'),'Current completed list remains visible when its group is collapsed')
 assert.ok(selected.indexOf('进行中的学习清单')<selected.indexOf('已结束的学习清单'),'Completed lists follow active lists')
 const reopened=sidebar('list-2',lists.map(list=>list.id===2?{...list,pending_count:1}:list))
 assert.ok(reopened.includes('已结束的学习清单')&&!reopened.includes('completed-list"'),'Reopening a task returns the list to the active group')
 assert.ok(!ordinary.match(/title="空清单"[^>]*completed-list/),'Empty lists are never marked completed')
 const records=renderToStaticMarkup(React.createElement(ViewTabs,{view:'trash',setView(){},kind:'records'}))
 for(const label of ['周复盘','导入历史','操作记录','回收站'])assert.ok(records.includes(label),`${label} remains reachable`)
 assert.match(records, /class="selected" aria-current="page">回收站/)
 const tasks=renderToStaticMarkup(React.createElement(ViewTabs,{view:'inbox',setView(){},kind:'tasks'}))
 assert.match(tasks, /class="selected" aria-current="page">收集箱/)
 const setup=React.createElement('button',null,'连接 Gmail')
 const connected=renderToStaticMarkup(React.createElement(GmailConnectionCard,{status:{connected:true,email:'demo@example.com'}},setup))
 assert.ok(connected.includes('已连接 demo@example.com')&&!connected.includes('<button'),'Connected Gmail exposes status only')
 const disconnected=renderToStaticMarkup(React.createElement(GmailConnectionCard,{status:{connected:false,authorization_required:true}},setup))
 assert.ok(disconnected.includes('授权已失效')&&disconnected.includes('<button'),'Expired authorization still exposes recovery')
 const loading=renderToStaticMarkup(React.createElement(GmailConnectionCard,{status:null},setup))
 assert.ok(loading.includes('正在读取连接状态')&&!loading.includes('<button'),'Loading does not show OAuth setup prematurely')
 console.log('UI regression checks passed: navigation, completion transitions, record/task tabs, Gmail states.')
}
