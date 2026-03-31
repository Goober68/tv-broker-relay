/**
 * API client.
 *
 * Handles:
 *   - JWT access token (stored in memory — never localStorage)
 *   - Automatic token refresh on 401
 *   - Consistent error shape
 */

let accessToken = null
let refreshPromise = null

export function setAccessToken(token) {
  accessToken = token
}

export function getAccessToken() {
  return accessToken
}

export function clearAccessToken() {
  accessToken = null
}

// ── Core fetch wrapper ─────────────────────────────────────────────────────────

async function request(path, options = {}, retry = true) {
  const isFormData = options.body instanceof FormData
  const headers = {
    ...(isFormData ? {} : { 'Content-Type': 'application/json' }),
    ...options.headers,
  }
  if (accessToken) {
    headers['Authorization'] = `Bearer ${accessToken}`
  }

  const res = await fetch(path, { ...options, headers })

  // Auto-refresh on 401 — but not for auth endpoints (login/register)
  if (res.status === 401 && retry && !path.includes('/auth/login') && !path.includes('/auth/register')) {
    const refreshed = await attemptRefresh()
    if (refreshed) {
      return request(path, options, false)
    }
    // Refresh failed — clear auth state
    clearAccessToken()
    window.dispatchEvent(new Event('auth:expired'))
    throw new ApiError(401, 'Session expired')
  }

  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const body = await res.json()
      detail = body.detail || JSON.stringify(body)
    } catch {}
    throw new ApiError(res.status, detail)
  }

  // 204 No Content
  if (res.status === 204) return null

  return res.json()
}

async function attemptRefresh() {
  // Deduplicate concurrent refresh calls
  if (refreshPromise) return refreshPromise
  refreshPromise = fetch('/api/auth/refresh', { method: 'POST', credentials: 'include' })
    .then(async (res) => {
      if (!res.ok) return false
      const data = await res.json()
      setAccessToken(data.access_token)
      return true
    })
    .catch(() => false)
    .finally(() => { refreshPromise = null })
  return refreshPromise
}

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail)
    this.status = status
    this.detail = detail
  }
}

// ── Convenience methods ────────────────────────────────────────────────────────

const api = {
  get:    (path, opts)   => request(path, { method: 'GET', ...opts }),
  post:   (path, body, opts) => request(path, { method: 'POST',  body: JSON.stringify(body), ...opts }),
  put:    (path, body, opts) => request(path, { method: 'PUT',   body: JSON.stringify(body), ...opts }),
  patch:  (path, body, opts) => request(path, { method: 'PATCH', body: JSON.stringify(body), ...opts }),
  delete: (path, opts)   => request(path, { method: 'DELETE', ...opts }),
}

// ── Auth ───────────────────────────────────────────────────────────────────────

export const auth = {
  register: (email, password) =>
    api.post('/api/auth/register', { email, password }),

  login: async (email, password) => {
    const data = await api.post('/api/auth/login', { email, password })
    setAccessToken(data.access_token)
    return data
  },

  logout: async () => {
    await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).catch(() => {})
    clearAccessToken()
  },

  logoutAll: () => api.post('/api/auth/logout-all'),

  me: () => api.get('/api/auth/me'),

  refresh: () => attemptRefresh(),

  changePassword: (email, password) =>
    api.put('/api/auth/me/password', { email, password }),
}

// ── Billing ────────────────────────────────────────────────────────────────────

export const billing = {
  subscription: () => api.get('/api/billing/subscription'),
  plans:        () => api.get('/api/billing/plans'),
  checkout:     (plan_name) => api.post('/api/billing/checkout', { plan_name }),
  portal:       () => api.post('/api/billing/portal'),
}

// ── API Keys ───────────────────────────────────────────────────────────────────

export const apiKeys = {
  list:   ()           => api.get('/api/api-keys'),
  create: (name)       => api.post('/api/api-keys', { name }),
  revoke: (id)         => api.delete(`/api/api-keys/${id}`),
}

// ── Broker Accounts ────────────────────────────────────────────────────────────

export const brokerAccounts = {
  list:   ()                     => api.get('/api/broker-accounts'),
  get:    (id)                   => api.get(`/api/broker-accounts/${id}`),
  create: (body)                 => api.post('/api/broker-accounts', body),
  update: (id, body)             => api.patch(`/api/broker-accounts/${id}`, body),
  delete: (id)                   => api.delete(`/api/broker-accounts/${id}`),
  fields: (broker)               => api.get(`/api/broker-accounts/fields/${broker}`),
  instruments:     (id)          => api.get(`/api/broker-accounts/${id}/instruments`),
  upsertInstrument:(id, sym, body)=> api.put(`/api/broker-accounts/${id}/instruments/${sym}`, body),
  deleteInstrument:(id, sym)     => api.delete(`/api/broker-accounts/${id}/instruments/${sym}`),
  updateDisplayName:(id, name)    => api.patch(`/api/broker-accounts/${id}/display-name`, { display_name: name }),
  importHistory:    (id)          => api.post(`/api/broker-accounts/${id}/import-history`),
  syncHistory:      (id)          => api.post(`/api/broker-accounts/${id}/sync-history`),
  importCsv:        (id, file)    => {
    const form = new FormData()
    form.append('file', file)
    return request(`/api/broker-accounts/${id}/import-csv`, { method: 'POST', body: form })
  },
  updateAutoClose: (id, body)    => api.patch(`/api/broker-accounts/${id}/auto-close`, body),
  updateDrawdown:  (id, body)    => api.patch(`/api/broker-accounts/${id}/drawdown-limits`, body),  // also handles commission_per_contract
  suspend:         (id, active)  => api.patch(`/api/broker-accounts/${id}/suspend`, { is_active: active }),
  flatten:         (id)          => api.post(`/api/broker-accounts/${id}/flatten`),
  updateFifo:      (id, body)    => api.patch(`/api/broker-accounts/${id}/fifo`, body),
  verifyConnection: (body)        => api.post('/api/broker-accounts/verify-connection', body),
  tradovateFetchAccounts: (creds) => api.post('/api/broker-accounts/tradovate/fetch-accounts', { credentials: creds }),
  tradovateOAuthUrl:      (env = 'live', reauth = false) => api.get(`/api/broker-accounts/tradovate/oauth-url?env=${env}${reauth ? '&reauth=true' : ''}`),
  tradovateReauth:        (token) => api.post('/api/broker-accounts/tradovate/reauth', { token }),
  tradovateBulkCreate:    (creds, accounts) => api.post('/api/broker-accounts/tradovate/bulk-create', { credentials: creds, accounts }),
}

// ── Orders & Positions ─────────────────────────────────────────────────────────

export const orders = {
  list:       (params = {}) => api.get('/api/orders?' + new URLSearchParams(params)),
  open:       (params = {}) => api.get('/api/orders/open?' + new URLSearchParams(params)),
  deliveries: (params = {}) => api.get('/api/webhook-deliveries?' + new URLSearchParams(params)),
}

export const pnl = {
  summary: (period = 'daily', start, end) => {
    const params = new URLSearchParams({ period })
    if (start) params.set('start', start)
    if (end) params.set('end', end)
    return api.get(`/api/pnl/summary?${params}`)
  },
}

export const positions = {
  list: (params = {}) => api.get('/api/positions?' + new URLSearchParams(params)),
}

// ── Admin ──────────────────────────────────────────────────────────────────────

export const admin = {
  tenants:     (params = {})        => api.get('/api/admin/tenants?' + new URLSearchParams(params)),
  tenant:      (id)                 => api.get(`/api/admin/tenants/${id}`),
  assignPlan:  (id, plan_name)      => api.post(`/api/admin/tenants/${id}/plan`, { plan_name }),
  setActive:   (id, is_active)      => api.patch(`/api/admin/tenants/${id}/active?is_active=${is_active}`),
  stats:       ()                   => api.get('/api/admin/stats'),
  plans:       ()                   => api.get('/api/admin/plans'),
  setPrice:    (id, stripe_price_id)=> api.patch(`/api/admin/plans/${id}/stripe-price?stripe_price_id=${stripe_price_id}`),
}

export default api
