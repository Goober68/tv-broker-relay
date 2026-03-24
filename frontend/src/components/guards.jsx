import { Navigate, useLocation } from 'react-router-dom'
import { useAuth } from '../lib/auth-context'
import { PageSpinner } from '../components/ui'

export function RequireAuth({ children }) {
  const { user, loading } = useAuth()
  const location = useLocation()

  if (loading) return <PageSpinner />
  if (!user) return <Navigate to="/login" state={{ from: location }} replace />
  return children
}

export function RequireAdmin({ children }) {
  const { user, loading } = useAuth()
  const location = useLocation()

  if (loading) return <PageSpinner />
  if (!user) return <Navigate to="/login" state={{ from: location }} replace />
  if (!user.is_admin) return <Navigate to="/dashboard" replace />
  return children
}

export function RequireGuest({ children }) {
  const { user, loading } = useAuth()

  if (loading) return <PageSpinner />
  if (user) return <Navigate to="/dashboard" replace />
  return children
}
