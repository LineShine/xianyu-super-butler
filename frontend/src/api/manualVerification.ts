import { del, get, post } from '@/utils/request'

const PREFIX = '/api/v1/risk-control-logs/manual-verification'

export interface ManualVerificationState {
  session_id: string
  account_id: string
  status: 'loading' | 'ready' | 'refresh_required' | 'success' | 'failed' | 'cancelled'
  message: string
  width: number
  height: number
  expires_at: number
  image?: string
  sequence?: number
}

export interface VerificationInput {
  kind: 'down' | 'move' | 'up' | 'wheel' | 'key'
  x?: number
  y?: number
  delta?: number
  key?: 'Tab' | 'Shift+Tab' | 'Enter' | 'Space' | 'ArrowLeft' | 'ArrowRight'
}

type Response = { success: boolean; message?: string }
type Result = Response & { data: ManualVerificationState }

// Backend business errors use HTTP 200; never treat them as accepted input.
const checked = async <T extends Response>(pending: Promise<T>): Promise<T> => {
  const result = await pending
  if (!result.success) throw new Error(result.message || '手动验证请求失败')
  return result
}

export const startManualVerification = (accountId: string, logId?: string) =>
  checked(post<Response & { data: { log_id: number } }>(PREFIX, { account_id: accountId, log_id: logId }))
export const getManualVerification = (sessionId: string) =>
  checked(get<Result>(`${PREFIX}/${sessionId}`, { timeout: 15000 }))
export const sendVerificationInput = (sessionId: string, sequence: number, events: VerificationInput[]) =>
  checked(post<Response>(`${PREFIX}/${sessionId}/input`, { sequence, events }, { timeout: 15000 }))
export const checkManualVerification = (sessionId: string) =>
  checked(post<Result>(`${PREFIX}/${sessionId}/check`, {}, { timeout: 30000 }))
export const refreshManualVerification = (sessionId: string) =>
  checked(post<Result>(`${PREFIX}/${sessionId}/refresh`, {}, { timeout: 60000 }))
export const closeManualVerification = (sessionId: string) => checked(del<Response>(`${PREFIX}/${sessionId}`, { timeout: 5000 }))

export const MANUAL_VERIFICATION_UPDATED = 'manual-verification-updated'
export interface VerificationQueueState {
  pending_count: number
  available: boolean
  account_id?: string
  account_name?: string
  expires_at?: number
  session?: ManualVerificationState
  message?: string
}
export const getVerificationQueue = (clientId: string) => checked(get<Response & { data: VerificationQueueState }>(`${PREFIX}/queue`, { params: { client_id: clientId } }))
export const claimVerificationQueue = (clientId: string) =>
  checked(post<Response & { data: VerificationQueueState }>(`${PREFIX}/queue/claim`, { client_id: clientId }))
