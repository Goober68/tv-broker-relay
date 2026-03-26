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
  const headers = {
    'Content-Type': 'application/json',
    ...options.headers,
  }
  if (accessToken) {
    headers['Authorization'] = `Bearer ${accessToken}`
  }

  const res = await fetch(path, { ...options, headers })

  // Auto-refresh on 401
  if (res.status === 401 && retry) {
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
  refreshPromise = fetch('/auth/refresh', { method: 'POST', credentials: 'include' })
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
    api.post('/auth/register', { email, password }),

  login: async (email, password) => {
    const data = await api.post('/auth/login', { email, password })
    setAccessToken(data.access_token)
    return data
  },

  logout: async () => {
    await fetch('/auth/logout', { method: 'POST', credentials: 'include' }).catch(() => {})
    clearAccessToken()
  },

  logoutAll: () => api.post('/auth/logout-all'),

  me: () => api.get('/auth/me'),

  refresh: () => attemptRefresh(),

  changePassword: (email, password) =>
    api.put('/auth/me/password', { email, password }),
}

// ── Billing ────────────────────────────────────────────────────────────────────

export const billing = {
  subscription: () => api.get('/billing/subscription'),
  plans:        () => api.get('/billing/plans'),
  checkout:     (plan_name) => api.post('/billing/checkout', { plan_name }),
  portal:       () => api.post('/billing/portal'),
}

// ── API Keys ───────────────────────────────────────────────────────────────────

export const apiKeys = {
  list:   ()           => api.get('/api-keys'),
  create: (name)       => api.post('/api-keys', { name }),
  revoke: (id)         => api.delete(`/api-keys/${id}`),
}

// ── Broker Accounts ────────────────────────────────────────────────────────────

export const brokerAccounts = {
  list:   ()                     => api.get('/broker-accounts'),
  get:    (id)                   => api.get(`/broker-accounts/${id}`),
  create: (body)                 => api.post('/broker-accounts', body),
  update: (id, body)             => api.patch(`/broker-accounts/${id}`, body),
  delete: (id)                   => api.delete(`/broker-accounts/${id}`),
  fields: (broker)               => api.get(`/broker-accounts/fields/${broker}`),
  instruments:     (id)          => api.get(`/broker-accounts/${id}/instruments`),
  upsertInstrument:(id, sym, body)=> api.put(`/broker-accounts/${id}/instruments/${sym}`, body),
  deleteInstrument:(id, sym)     => api.delete(`/broker-accounts/${id}/instruments/${sym}`),
}

// ── Orders & Positions ─────────────────────────────────────────────────────────

export const orders = {
  list:       (params = {}) => api.get('/api/orders?' + new URLSearchParams(params)),
  open:       (params = {}) => api.get('/api/orders/open?' + new URLSearchParams(params)),
  deliveries: (params = {}) => api.get('/api/webhook-deliveries?' + new URLSearchParams(params)),
}

export const pnl = {
  summary: (period = 'daily') => apiFetch(`/api/pnl/summary?period=${period}`),
}

export const positions = {
  list: (params = {}) => api.get('/api/positions?' + new URLSearchParams(params)),
}

// ── Admin ──────────────────────────────────────────────────────────────────────

export const admin = {
  tenants:     (params = {})        => api.get('/admin/tenants?' + new URLSearchParams(params)),
  tenant:      (id)                 => api.get(`/admin/tenants/${id}`),
  assignPlan:  (id, plan_name)      => api.post(`/admin/tenants/${id}/plan`, { plan_name }),
  setActive:   (id, is_active)      => api.patch(`/admin/tenants/${id}/active?is_active=${is_active}`),
  stats:       ()                   => api.get('/admin/stats'),
  plans:       ()                   => api.get('/admin/plans'),
  setPrice:    (id, stripe_price_id)=> api.patch(`/admin/plans/${id}/stripe-price?stripe_price_id=${stripe_price_id}`),
}

export default api
