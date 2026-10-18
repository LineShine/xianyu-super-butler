import { useEffect, useState } from 'react'
import { Save, Key, RefreshCw } from 'lucide-react'
import { getUserSetting, updateUserSetting, changePassword, getSecretKey, resetSecretKey, testLocationChatRemoteApi } from '@/api/settings'
import { useUIStore } from '@/store/uiStore'
import { useAuthStore } from '@/store/authStore'
import { getApiErrorMessage } from '@/utils/request'

const fields = [
  {key:'contact_wechat', label:'微信联系方式', help:'用于您自己的客服联系方式'},
  {key:'contact_qq', label:'QQ 联系方式', help:'用于您自己的客服联系方式'},
  {key:'redelivery_trigger_keyword', label:'重发货触发关键词', help:'买家发送此关键词时按账号配置触发重发货'},
  {key:'location_chat.remote_url', label:'位置消息服务地址', help:'按需填写您使用的外部服务地址'},
  {key:'location_chat.remote_secret_key', label:'位置消息服务密钥', secret:true},
]
export function PersonalSettings() {
  const {user,clearAuth}=useAuthStore();const {addToast}=useUIStore()
  const [values,setValues]=useState<Record<string,string>>({}); const [saving,setSaving]=useState(false)
  const [apiKey,setApiKey]=useState('');const [current,setCurrent]=useState('');const [password,setPassword]=useState('');const [confirm,setConfirm]=useState('')
  const report=(e:unknown)=>addToast({type:'error',message:getApiErrorMessage(e,'操作失败')})
  useEffect(()=>{Promise.all(fields.map(async f=>[f.key,(await getUserSetting(f.key)).value||''])).then(v=>setValues(Object.fromEntries(v))).catch(report);getSecretKey().then(v=>setApiKey(v.secret_key||'')).catch(report)},[])
  async function save(){setSaving(true);try {await Promise.all(fields.map(f=>updateUserSetting(f.key,values[f.key]||'')));addToast({type:'success',message:'个人设置已保存'})}catch(e){report(e)}finally{setSaving(false)}}
  async function change(){if(password.length<6||password!==confirm){addToast({type:'error',message:'请填写至少 6 位且两次一致的新密码'});return}setSaving(true);try {const res=await changePassword({current_password:current,new_password:password});if(!res.success)throw new Error(res.message);clearAuth();addToast({type:'success',message:'密码已更新，请重新登录'})}catch(e){report(e)}finally{setSaving(false)}}
  return <div className="space-y-6"><div className="page-header"><h1 className="page-title">个人设置</h1><p className="page-description">{user?.username} · 管理您的偏好和访问凭据</p></div>
    <section className="card p-8"><h2 className="text-lg font-bold mb-6">客服与自动化偏好</h2><div className="grid md:grid-cols-2 gap-6">{fields.map(f=><label key={f.key} className="block"><span className="input-label">{f.label}</span><input className="input-ios w-full" type={f.secret?'password':'text'} value={values[f.key]||''} onChange={e=>setValues({...values,[f.key]:e.target.value})}/><span className="text-xs text-gray-400">{f.help}</span></label>)}</div><div className="flex gap-3 mt-6"><button className="btn-ios-primary" disabled={saving} onClick={save}><Save size={16}/>保存设置</button><button className="btn-ios-secondary" disabled={saving} onClick={async()=>{setSaving(true);try{const v=await testLocationChatRemoteApi(values['location_chat.remote_url']||'', values['location_chat.remote_secret_key']||'');addToast({type:v.success?'success':'error',message:v.message||'测试完成'})}catch(e){report(e)}finally{setSaving(false)}}}>测试位置消息服务</button></div></section>
    <section className="card p-8"><h2 className="text-lg font-bold mb-3 flex gap-2"><Key size={20}/>业务 API 密钥</h2><p className="text-gray-500 text-sm mb-4">供您授权的账号、订单及发布接口使用。</p><div className="flex gap-3"><input className="input-ios flex-1" readOnly type="password" value={apiKey}/><button className="btn-ios-secondary" onClick={async()=>{try{await navigator.clipboard.writeText(apiKey);addToast({type:'success',message:'已复制密钥'})}catch(e){report(e)}}}>复制</button><button className="btn-ios-secondary" onClick={async()=>{if(!window.confirm('更换后旧密钥将失效，是否继续？'))return;try{const v=await resetSecretKey();if(!v.success)throw new Error(v.message);const key=await getSecretKey();setApiKey(key.secret_key||'');addToast({type:'success',message:'密钥已更换'})}catch(e){report(e)}}}><RefreshCw size={16}/>更换</button></div></section>
    <section className="card p-8"><h2 className="text-lg font-bold mb-6">修改登录密码</h2><div className="grid md:grid-cols-3 gap-4">{[{label:'当前密码',value:current,set:setCurrent},{label:'新密码',value:password,set:setPassword},{label:'确认新密码',value:confirm,set:setConfirm}].map(f=><label key={f.label}><span className="input-label">{f.label}</span><input className="input-ios w-full" type="password" value={f.value} onChange={e=>f.set(e.target.value)}/></label>)}</div><button className="btn-ios-primary mt-6" disabled={saving} onClick={change}>更新密码</button></section>
  </div>
}
