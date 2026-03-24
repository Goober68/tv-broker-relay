import { createContext, useContext, useState, useEffect, useCallback } from 'react'
import { auth as authApi, setAccessToken, clearAccessToken } from '../lib/api'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser]       = useState(null)    // { id, email, is_admin, plan_name }
  const [loading, setLoading] = useState(true)    // true during initial session check

  // Try to restore session from refresh token cookie on mount
  useEffect(() => {
    authApi.refresh()
      .then(ok => ok ? authApi.me() : null)
      .then(me => { if (me) setUser(me) })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  // Listen for forced session expiry (401 with no successful refresh)
  useEffect(() => {
    const handler = () => { setUser(null) }
    window.addEventListener('auth:expired', handler)
    return () => window.removeEventListener('auth:expired', handler)
  }, [])

  const login = useCallback(async (email, password) => {
    const data = await authApi.login(email, password)
    // setAccessToken already called inside authApi.login
    const me = await authApi.me()
    setUser(me)
    return me
  }, [])

  const logout = useCallback(async () => {
    await authApi.logout()
    setUser(null)
  }, [])

  const register = useCallback(async (email, password) => {
    // Register returns the tenant object, then auto-login
    await authApi.register(email, password)
    return login(email, password)
  }, [login])

  const refreshUser = useCallback(async () => {
    try {
      const me = await authApi.me()
      setUser(me)
      return me
    } catch {
      return null
    }
  }, [])

  return (
    <AuthContext.Provider value={{ user, loading, login, logout, register, refreshUser }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
