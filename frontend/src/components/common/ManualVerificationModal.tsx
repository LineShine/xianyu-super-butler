import { useEffect, useRef, useState, type PointerEvent } from 'react'
import { createPortal } from 'react-dom'
import { Link } from 'react-router-dom'
import { CheckCircle, ExternalLink, Loader2, ShieldAlert, X, XCircle } from 'lucide-react'
import {
  getManualVerification, closeManualVerification,
  checkManualVerification, refreshManualVerification, sendVerificationInput,
  type ManualVerificationState, type VerificationInput,
} from '@/api/manualVerification'
import { getApiErrorMessage } from '@/utils/request'

interface Props {
  accountId: string
  accountName: string
  initialState: ManualVerificationState
  pendingCount?: number
  onClose: () => void
}

export function ManualVerificationModal({ accountId, accountName, initialState, pendingCount = 1, onClose }: Props) {
  const [state, setState] = useState<ManualVerificationState>(initialState)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [remaining, setRemaining] = useState(Math.max(0, Math.ceil(initialState.expires_at - Date.now() / 1000)))
  const [checking, setChecking] = useState(false)
  const [inputFailed, setInputFailed] = useState(false)
  const sessionId = useRef(initialState.session_id)
  const alive = useRef(true)
  const dialog = useRef<HTMLDivElement>(null)
  const queue = useRef<VerificationInput[]>([])
  const sending = useRef<Promise<void> | null>(null)
  const sequence = useRef(initialState.sequence || 0)
  const autoCheckTimer = useRef<ReturnType<typeof setTimeout>>()
  const lastCheck = useRef(0)
  const pointer = useRef<number | null>(null)
  const lastMove = useRef(0)
  const checkInProgress = useRef(false)
  const closeRef = useRef(onClose)
  closeRef.current = onClose

  useEffect(() => {
    alive.current = true
    const previousFocus = document.activeElement as HTMLElement | null
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    dialog.current?.focus()
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeRef.current()
      if (event.key !== 'Tab' || event.defaultPrevented) return
      const elements = dialog.current?.querySelectorAll<HTMLElement>(
        'button:not(:disabled), a[href], [tabindex="0"]',
      )
      if (!elements?.length) return
      const first = elements[0]
      const last = elements[elements.length - 1]
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', keyboard)
    return () => {
      alive.current = false
      document.body.style.overflow = previousOverflow
      document.removeEventListener('keydown', keyboard)
      previousFocus?.focus()
      clearTimeout(autoCheckTimer.current)
      // StrictMode replays effects; defer cleanup so its immediate remount does
      // not close the server session that is about to be displayed.
      setTimeout(() => {
        if (!alive.current && sessionId.current) void closeManualVerification(sessionId.current).catch(() => {})
      }, 0)
    }
  }, [])

  useEffect(() => {
    const tick = () => {
      const seconds = Math.max(0, Math.ceil(state.expires_at - Date.now() / 1000))
      setRemaining(seconds)
      if (seconds === 0) closeRef.current()
    }
    tick()
    const timer = setInterval(tick, 250)
    return () => clearInterval(timer)
  }, [state.expires_at])

  useEffect(() => {
    if (!['success', 'failed', 'cancelled'].includes(state.status)) return
    const timer = setTimeout(() => closeRef.current(), 1500)
    return () => clearTimeout(timer)
  }, [state.status])

  useEffect(() => {
    if (!state || !['loading', 'ready', 'refresh_required'].includes(state.status)) return
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      if (checkInProgress.current) {
        timer = setTimeout(poll, 500)
        return
      }
      try {
        const result = await getManualVerification(sessionId.current)
        if (stopped || !alive.current) return
        if (!checkInProgress.current) {
          setState(previous => ({ ...previous, ...result.data }))
          setRemaining(Math.max(0, Math.ceil(result.data.expires_at - Date.now() / 1000)))
        }
      } catch (reason) {
        if (!stopped && alive.current) {
          setError(getApiErrorMessage(reason, '验证页面连接中断，请关闭窗口后重试'))
          setInputFailed(true)
        }
        return
      }
      if (!stopped) timer = setTimeout(poll, 450)
    }
    timer = setTimeout(poll, 200)
    return () => { stopped = true; clearTimeout(timer) }
  }, [state?.session_id, state?.status])

  const flush = (): Promise<void> => {
    if (sending.current) return sending.current
    const task = (async () => {
      while (queue.current.length && alive.current) {
        const events = queue.current.splice(0, 64)
        await sendVerificationInput(sessionId.current, ++sequence.current, events)
      }
    })()
    sending.current = task
    void task.catch(reason => {
      queue.current = []
      if (alive.current) {
        setInputFailed(true)
        setError(getApiErrorMessage(reason, '鼠标操作传输失败，请关闭窗口后重试'))
      }
    }).finally(() => { sending.current = null })
    return task
  }

  const enqueue = (event: VerificationInput) => {
    if (inputFailed || checking || state?.status !== 'ready') return
    queue.current.push(event)
    void flush().catch(() => {})
  }

  const position = (event: PointerEvent<HTMLImageElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    return {
      x: Math.max(0, Math.min(900, (event.clientX - rect.left) / rect.width * 900)),
      y: Math.max(0, Math.min(560, (event.clientY - rect.top) / rect.height * 560)),
    }
  }

  const release = (event: PointerEvent<HTMLImageElement>) => {
    if (pointer.current !== event.pointerId) return
    enqueue({ kind: 'up', ...position(event) })
    pointer.current = null
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
    clearTimeout(autoCheckTimer.current)
    autoCheckTimer.current = setTimeout(() => { void check() }, Math.max(1200, 10100 - (Date.now() - lastCheck.current)))
  }

  const check = async () => {
    if (pointer.current !== null || checkInProgress.current || !alive.current || state.status !== 'ready') return
    clearTimeout(autoCheckTimer.current)
    lastCheck.current = Date.now()
    checkInProgress.current = true
    setChecking(true)
    setError('')
    try {
      await flush()
      const result = await checkManualVerification(sessionId.current)
      if (alive.current) setState(previous => ({ ...previous, ...result.data }))
    } catch (reason) {
      if (alive.current) setError(getApiErrorMessage(reason, '检查验证结果失败'))
    } finally {
      checkInProgress.current = false
      if (alive.current) setChecking(false)
    }
  }

  const refresh = async () => {
    setBusy(true)
    setError('')
    try {
      const result = await refreshManualVerification(sessionId.current)
      if (alive.current) setState(result.data)
    } catch (reason) {
      if (alive.current) setError(getApiErrorMessage(reason, '获取验证页面失败'))
    } finally {
      if (alive.current) setBusy(false)
    }
  }

  const ready = state?.status === 'ready'
  const successful = state?.status === 'success'
  const failed = state?.status === 'failed' || state?.status === 'cancelled' || inputFailed

  return createPortal(
    <div className="fixed inset-0 z-[220] flex items-center justify-center bg-slate-950/50 backdrop-blur-sm p-3 sm:p-6">
      <div ref={dialog} role="dialog" aria-modal="true" aria-labelledby="manual-verification-title" tabIndex={-1}
        className="flex max-h-[92vh] w-full max-w-5xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl outline-none dark:bg-slate-800">
        {/* Header/footer stay visible; the official page scrolls inside the body. */}
        <div className="flex shrink-0 items-center justify-between gap-4 border-b border-slate-100 px-5 py-4 dark:border-slate-700">
          <div className="flex items-center gap-3">
            <span className="rounded-xl bg-red-50 p-2.5 text-red-600 dark:bg-red-950/40"><ShieldAlert className="h-6 w-6" /></span>
            <div>
              <h2 id="manual-verification-title" className="text-lg font-semibold text-slate-900 dark:text-white">手动完成闲鱼验证</h2>
              <p className="mt-1 text-sm text-slate-500">{accountName} · 后续等待 {Math.max(0, pendingCount - 1)} 个</p>
            </div>
          </div>
          <span role="timer" aria-label="验证剩余时间" className={`shrink-0 rounded-xl px-3 py-2 text-sm font-semibold tabular-nums ${remaining <= 15 ? 'bg-red-100 text-red-700' : 'bg-blue-50 text-blue-700'}`}>倒计时 <span className="text-xl">{remaining}</span> 秒</span>
          <button onClick={onClose} aria-label="关闭手动验证" className="rounded-lg p-2 text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-700"><X className="h-5 w-5" /></button>
        </div>
        <div className="min-h-0 overflow-y-auto p-5">
          <div className="mb-4 rounded-xl border border-blue-100 bg-blue-50 px-4 py-3 text-sm leading-6 text-blue-800 dark:border-blue-900 dark:bg-blue-950/30 dark:text-blue-200">
            请在下方手动拖动滑块，松开后自动检查结果，验证成功后自动启用账号并恢复连接。倒计时结束或手动关闭弹窗，都会继续下一项；未通过验证的账号保持暂停，其他账号正常运行。
          </div>
          {error && <div role="alert" className="mb-4 flex items-start gap-2 rounded-xl bg-red-50 p-3 text-sm text-red-700 dark:bg-red-950/30 dark:text-red-300"><XCircle className="mt-0.5 h-4 w-4 shrink-0" />{error}</div>}
          {state?.status === 'loading' && <div className="flex min-h-[280px] flex-col items-center justify-center gap-4 text-slate-500"><Loader2 className="h-8 w-8 animate-spin" /><p>{state.message}</p></div>}
          {state?.status === 'refresh_required' && (
            <div role="status" className="flex min-h-[230px] flex-col items-center justify-center gap-4 rounded-xl bg-amber-50 p-6 text-center text-amber-800 dark:bg-amber-950/30 dark:text-amber-200">
              <ShieldAlert className="h-10 w-10" /><p>{state.message}</p>
              <button onClick={refresh} disabled={busy} className="btn-ios-primary flex items-center gap-2">{busy && <Loader2 className="h-4 w-4 animate-spin" />}继续打开验证</button>
            </div>
          )}
          {ready && !failed && (
            <div className="overflow-hidden rounded-xl border border-slate-200 bg-slate-50 dark:border-slate-600">
              <div className="flex items-center justify-between gap-3 border-b border-slate-200 px-3 py-2 text-xs text-slate-500 dark:border-slate-600 dark:bg-slate-900">
                <span>闲鱼官方验证页面 · 点击或拖动下方画面</span><span>剩余 {Math.floor(remaining / 60)}:{String(remaining % 60).padStart(2, '0')}</span>
              </div>
              {state.image ? <img src={state.image} alt="可手动操作的闲鱼验证页面" draggable={false} tabIndex={0}
                className={`block h-auto w-full touch-none select-none outline-blue-400 ${checking ? 'pointer-events-none opacity-60' : ''}`}
                onContextMenu={event => event.preventDefault()}
                onPointerDown={event => {
                  if (event.button !== 0 || pointer.current !== null) return
                  event.preventDefault()
                  event.currentTarget.focus()
                  event.currentTarget.setPointerCapture(event.pointerId)
                  pointer.current = event.pointerId
                  enqueue({ kind: 'down', ...position(event) })
                }}
                onPointerMove={event => {
                  if (pointer.current !== event.pointerId || performance.now() - lastMove.current < 25) return
                  lastMove.current = performance.now()
                  enqueue({ kind: 'move', ...position(event) })
                }}
                onPointerUp={release} onPointerCancel={release} onLostPointerCapture={release}
                onWheel={event => enqueue({ kind: 'wheel', delta: Math.max(-560, Math.min(560, event.deltaY)) })}
                onKeyDown={event => {
                  // Tab remains local so keyboard users can always leave the viewport.
                  const keys = ['Enter', ' ', 'ArrowLeft', 'ArrowRight']
                  if (!keys.includes(event.key)) return
                  event.preventDefault()
                  enqueue({ kind: 'key', key: (event.key === ' ' ? 'Space' : event.key) as VerificationInput['key'] })
                }}
              /> : <div className="flex h-64 items-center justify-center"><Loader2 className="h-6 w-6 animate-spin text-slate-400" /></div>}
            </div>
          )}
          {successful && <div role="status" className="flex min-h-[230px] flex-col items-center justify-center gap-4 rounded-xl bg-green-50 p-6 text-center text-green-800 dark:bg-green-950/30 dark:text-green-200"><CheckCircle className="h-12 w-12" /><p className="font-medium">{state.message}</p></div>}
          {failed && <div role="alert" className="flex min-h-[230px] flex-col items-center justify-center gap-4 rounded-xl bg-red-50 p-6 text-center text-red-700 dark:bg-red-950/30 dark:text-red-300"><XCircle className="h-12 w-12" /><p>{inputFailed ? '验证连接已中断，请关闭窗口后重新打开。' : state?.message}</p></div>}
          {ready && !failed && <p role="status" className="mt-3 text-sm leading-6 text-slate-500">{state.message} 如提示链接过期或需要登录，请关闭窗口，稍后重试或在闲鱼 App 处理。</p>}
        </div>
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-t border-slate-100 px-5 py-4 dark:border-slate-700">
          <Link to={`/accounts?account_id=${encodeURIComponent(accountId)}`} onClick={onClose} className="flex items-center gap-1 text-sm text-blue-600">前往账号管理<ExternalLink className="h-3.5 w-3.5" /></Link>
          <div className="flex gap-3">
            <button onClick={onClose} className="btn-ios-secondary">跳过此项</button>
            {ready && !failed && <button onClick={check} disabled={checking} className="btn-ios-primary flex items-center gap-2">{checking && <Loader2 className="h-4 w-4 animate-spin" />}检查验证结果</button>}
          </div>
        </div>
      </div>
    </div>, document.body,
  )
}
