import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  claimVerificationQueue, closeManualVerification, getVerificationQueue,
  MANUAL_VERIFICATION_UPDATED, type VerificationQueueState,
} from '@/api/manualVerification'
import { ManualVerificationModal } from '@/components/common/ManualVerificationModal'
import { useAuthStore } from '@/store/authStore'
import { useUIStore } from '@/store/uiStore'
import { ACCOUNT_RISK_UPDATED } from './AccountRiskBanner'

// Each tab has its own claim. The backend also serializes claims across users.
const clientId = crypto.randomUUID()

export function ManualVerificationQueue() {
  const { isAuthenticated, _hasHydrated } = useAuthStore()
  const { addToast } = useUIStore()
  const navigate = useNavigate()
  const [active, setActive] = useState<VerificationQueueState | null>(null)
  const [pending, setPending] = useState(0)
  const activeRef = useRef<VerificationQueueState | null>(null)
  const closing = useRef(false)

  useEffect(() => {
    if (!_hasHydrated || !isAuthenticated) return
    let disposed = false
    let running = false
    const poll = async () => {
      if (running || disposed || closing.current) return
      running = true
      try {
        const queue = await getVerificationQueue(clientId)
        if (disposed) return
        setPending(queue.data.pending_count)
        if (activeRef.current || !queue.data.available) return
        const claim = await claimVerificationQueue(clientId)
        if (disposed) return // The next mounted controller may reclaim this tab's session.
        if (claim.data.message) addToast({ type: 'error', message: claim.data.message })
        if (!claim.data.session) return
        activeRef.current = claim.data
        setActive(claim.data)
        navigate('/risk-logs')
        window.dispatchEvent(new Event(ACCOUNT_RISK_UPDATED))
      } catch {
        // Polling never calls the official service or retries a failed challenge.
        // Server-side deadlines still advance if this browser loses connectivity.
      } finally {
        running = false
      }
    }
    void poll()
    const timer = setInterval(poll, 1500)
    window.addEventListener(MANUAL_VERIFICATION_UPDATED, poll)
    window.addEventListener('focus', poll)
    return () => {
      disposed = true
      clearInterval(timer)
      window.removeEventListener(MANUAL_VERIFICATION_UPDATED, poll)
      window.removeEventListener('focus', poll)
    }
  }, [_hasHydrated, isAuthenticated, navigate, addToast])

  const close = () => {
    if (closing.current || !activeRef.current?.session) return
    const session = activeRef.current.session
    closing.current = true
    // Hide immediately at the deadline, even if the network is unavailable.
    setActive(null)
    if (session.expires_at <= Date.now() / 1000) {
      addToast({ type: 'info', message: '本项 60 秒处理时间已结束，正在继续下一项' })
    }
    void closeManualVerification(session.session_id).catch(() => {}).finally(() => {
      activeRef.current = null
      closing.current = false
      window.dispatchEvent(new Event(MANUAL_VERIFICATION_UPDATED))
      window.dispatchEvent(new Event(ACCOUNT_RISK_UPDATED))
    })
  }

  return active?.session ? <ManualVerificationModal
    key={active.session.session_id}
    initialState={active.session}
    accountId={active.account_id!}
    accountName={active.account_name || active.account_id!}
    pendingCount={pending}
    onClose={close}
  /> : null
}
