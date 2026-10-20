import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowUpRight, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, XCircle } from 'lucide-react'
import { get } from '@/utils/request'

interface PausedAccount {
  account_id: string
  display_name: string
  consecutive_failures: number
  paused_at: string
  issue?: string
}

export const ACCOUNT_RISK_UPDATED = 'account-risk-updated'

/** Kept outside the route outlet; only a confirmed recovery removes an alert. */
export function AccountRiskBanner() {
  const [accounts, setAccounts] = useState<PausedAccount[]>([])
  const [index, setIndex] = useState(0)
  const [stale, setStale] = useState(false)
  // Remember this incident's choice across routes/polls; new incidents start expanded.
  const [collapsedIncidents, setCollapsedIncidents] = useState<string[]>([])

  useEffect(() => {
    let disposed = false
    let pending = false
    const refresh = async () => {
      if (pending) return
      pending = true
      try {
        const result = await get<{ success: boolean; data: PausedAccount[] }>('/api/v1/risk-control-logs/paused-accounts')
        if (!disposed) {
          if (result.success) {
            setAccounts(result.data)
            setStale(false)
          } else setStale(true)
        }
      } catch {
        if (!disposed) setStale(true)
      } finally {
        pending = false
      }
    }
    void refresh()
    const timer = window.setInterval(refresh, 10000)
    window.addEventListener('focus', refresh)
    window.addEventListener(ACCOUNT_RISK_UPDATED, refresh)
    return () => {
      disposed = true
      window.clearInterval(timer)
      window.removeEventListener('focus', refresh)
      window.removeEventListener(ACCOUNT_RISK_UPDATED, refresh)
    }
  }, [])

  if (!accounts.length) return null
  const activeIndex = Math.min(index, accounts.length - 1)
  const account = accounts[activeIndex]
  const incidentKey = `${account.account_id}:${account.paused_at}`
  const collapsed = collapsedIncidents.includes(incidentKey)
  const toggleCollapsed = () => setCollapsedIncidents(current => (
    collapsed ? current.filter(key => key !== incidentKey) : [...current, incidentKey]
  ))
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-2 px-3 pb-3 pt-2 sm:px-6 md:grid-cols-[5rem_minmax(0,1fr)_5rem] md:gap-3" role="region" aria-label="账号风险提醒">
      <div id="account-risk-details" className="w-full max-w-3xl justify-self-center overflow-hidden rounded-2xl border border-red-300/80 bg-red-50/95 shadow-lg shadow-red-900/10 backdrop-blur-xl dark:border-red-800 dark:bg-slate-800/95 md:col-start-2">
        <Link
          to={`/accounts?account_id=${encodeURIComponent(account.account_id)}`}
          className={`group flex gap-3 transition-colors hover:bg-red-100/60 focus-visible:outline focus-visible:outline-2 focus-visible:outline-red-600 dark:hover:bg-slate-700/60 ${collapsed ? 'items-center px-4 py-2.5' : 'p-4'}`}
        >
          <div className={`flex shrink-0 items-center justify-center bg-red-600 text-white ${collapsed ? 'h-8 w-8 rounded-lg' : 'h-10 w-10 rounded-xl'}`}>
            <XCircle size={collapsed ? 20 : 24} aria-hidden="true" />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1" role="status" aria-live="polite">
              <span className={`font-semibold text-red-950 dark:text-red-100 ${collapsed ? 'text-sm' : ''}`}>
                {collapsed ? '账号异常 · 自动重试已暂停' : '账号保护已启动 · 自动重试已暂停'}
              </span>
              {!collapsed && <span className="rounded-full bg-red-200/70 px-2 py-0.5 text-xs font-medium text-red-800 dark:bg-red-900/50 dark:text-red-200">需要处理</span>}
            </div>
            <p className={collapsed ? 'truncate text-xs text-red-800 dark:text-red-200' : 'mt-1 break-words text-sm text-red-950 dark:text-red-100'}>
              {account.display_name}{!collapsed && `（${account.account_id}）`} · {account.issue || `连续 ${account.consecutive_failures} 次滑块验证失败`}
            </p>
            {!collapsed && <>
              <p className="mt-1 text-sm leading-relaxed text-red-900/80 dark:text-slate-300">
                可在风控日志完成手动验证，通过后账号自动启用。若验证页面无法操作，请先在闲鱼 App 完成安全验证，再回到账号管理启用。暂停期间，该账号的自动回复与发货暂不可用。
              </p>
              <span className="mt-2 inline-flex items-center gap-1 text-sm font-semibold text-red-800 dark:text-red-200">
                前往处理 <ArrowUpRight size={16} aria-hidden="true" />
              </span>
            </>}
          </div>
          {collapsed && <span className="inline-flex shrink-0 items-center gap-1 text-xs font-semibold text-red-800 dark:text-red-200">处理 <ArrowUpRight size={14} aria-hidden="true" /></span>}
        </Link>
        {(accounts.length > 1 || stale) && (
          <div className="flex items-center justify-between gap-2 border-t border-red-200/70 px-4 py-2 text-xs text-red-800 dark:border-slate-700 dark:text-red-200">
            <span>{stale ? '状态更新暂不可用，保留上次风险提示' : '每个账号独立暂停，请分别处理'}</span>
            {accounts.length > 1 && <div className="flex shrink-0 items-center gap-2">
              <button type="button" aria-label="上一个异常账号" className="rounded p-1 hover:bg-red-200 dark:hover:bg-slate-700" onClick={() => setIndex((activeIndex + accounts.length - 1) % accounts.length)}><ChevronLeft size={16} /></button>
              <span>{activeIndex + 1} / {accounts.length}</span>
              <button type="button" aria-label="下一个异常账号" className="rounded p-1 hover:bg-red-200 dark:hover:bg-slate-700" onClick={() => setIndex((activeIndex + 1) % accounts.length)}><ChevronRight size={16} /></button>
            </div>}
          </div>
        )}
      </div>
      <button
        type="button"
        onClick={toggleCollapsed}
        aria-expanded={!collapsed}
        aria-controls="account-risk-details"
        aria-label={collapsed ? '展开风险提示' : '收起风险提示'}
        className="mt-1 inline-flex min-h-9 items-center justify-center gap-1 justify-self-end whitespace-nowrap rounded-xl border border-red-200 bg-white px-3 py-2 text-xs font-medium text-red-700 shadow-sm transition-colors hover:bg-red-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-red-600 dark:border-red-800 dark:bg-slate-800 dark:text-red-200 dark:hover:bg-slate-700 md:col-start-3"
      >
        {collapsed ? <ChevronDown size={16} aria-hidden="true" /> : <ChevronUp size={16} aria-hidden="true" />}
        {collapsed ? '展开' : '收起'}
      </button>
    </div>
  )
}
