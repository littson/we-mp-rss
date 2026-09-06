import http from './http'

export interface WereadAuthStatus {
  state: 'idle' | 'preparing' | 'waiting_scan' | 'confirmed' | 'expired' | 'failed'
  login_status: boolean
  qr_code?: string | null
  error?: string | null
  updated_at?: number | null
}

export interface WereadSubscriptionParams {
  mp_name: string
  mp_id: string
  avatar?: string
  mp_cover?: string
  mp_intro?: string
}

export interface WereadShelfAddResult {
  succ: number
  book_ids: string[]
  added_book_ids: string[]
  existing_book_ids: string[]
  added_count: number
  existing_count: number
  skipped_count?: number
  total: number
}

export const startWereadLogin = (force = false) => {
  return http.get<WereadAuthStatus>('/wx/weread/auth/qr/code', {
    params: { force }
  })
}

export const getWereadLoginStatus = () => {
  return http.get<WereadAuthStatus>('/wx/weread/auth/qr/status')
}

export const unbindWeread = () => {
  return http.post<WereadAuthStatus>('/wx/weread/auth/unbind')
}

export const addWereadShelfBooks = (bookIds: string[]) => {
  return http.post<WereadShelfAddResult>('/wx/weread/shelf/add', { book_ids: bookIds })
}

export const addAllWereadShelfBooks = () => {
  return http.post<WereadShelfAddResult>('/wx/weread/shelf/add-all')
}

export const addWereadSubscription = (data: WereadSubscriptionParams) => {
  return http.post('/wx/weread/subscriptions', data)
}
