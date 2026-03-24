import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../lib/auth-context'
import { Alert, Spinner } from '../components/ui'

export default function RegisterPage() {
  const { register } = useAuth()
  const navigate = useNavigate()

  const [email, setEmail]       = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm]   = useState('')
  const [loading, setLoading]   = useState(false)
  const [error, setError]       = useState(null)

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (password !== confirm) {
      setError('Passwords do not match')
      return
    }
    if (password.length < 8) {
      setError('Password must be at least 8 characters')
      return
    }
    if (!/\d/.test(password)) {
      setError('Password must contain at least one digit')
      return
    }
    setLoading(true)
    setError(null)
    try {
      await register(email, password)
      navigate('/dashboard', { replace: true })
    } catch (err) {
      setError(err.detail || 'Registration failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-5">
      <div>
        <h2 className="font-display font-bold text-xl text-base-50 mb-1">Create account</h2>
        <p className="text-sm text-base-400">Start on the Free plan. Upgrade anytime.</p>
      </div>

      <Alert type="error" message={error} />

      <div className="space-y-3">
        <div>
          <label className="block text-xs font-medium text-base-300 mb-1.5">Email</label>
          <input
            type="email"
            className="input"
            placeholder="you@example.com"
            value={email}
            onChange={e => setEmail(e.target.value)}
            required
            autoFocus
            autoComplete="email"
          />
        </div>

        <div>
          <label className="block text-xs font-medium text-base-300 mb-1.5">Password</label>
          <input
            type="password"
            className="input"
            placeholder="Min 8 chars, at least one digit"
            value={password}
            onChange={e => setPassword(e.target.value)}
            required
            autoComplete="new-password"
          />
        </div>

        <div>
          <label className="block text-xs font-medium text-base-300 mb-1.5">Confirm password</label>
          <input
            type="password"
            className="input"
            placeholder="••••••••"
            value={confirm}
            onChange={e => setConfirm(e.target.value)}
            required
            autoComplete="new-password"
          />
        </div>
      </div>

      {/* Plan summary */}
      <div className="bg-base-800 border border-base-700 rounded-md p-3 space-y-1">
        <div className="text-xs font-medium text-base-300 mb-2">Free plan includes:</div>
        {[
          '1 broker account',
          '50 orders / month',
          'Market orders only',
          '5 webhook calls / minute',
        ].map(item => (
          <div key={item} className="flex items-center gap-2 text-xs text-base-400">
            <span className="text-accent">✓</span>
            {item}
          </div>
        ))}
      </div>

      <button
        type="submit"
        className="btn-primary w-full flex items-center justify-center gap-2"
        disabled={loading}
      >
        {loading && <Spinner size="sm" />}
        {loading ? 'Creating account…' : 'Create account'}
      </button>

      <p className="text-xs text-base-500 text-center">
        By signing up you agree to our Terms of Service.
      </p>
    </form>
  )
}
