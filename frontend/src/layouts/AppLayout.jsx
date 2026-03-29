import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useState } from 'react'
import { clsx } from 'clsx'
import { useAuth } from '../lib/auth-context'

const NAV_ITEMS = [
  { to: '/dashboard',     label: 'Dashboard',       icon: <GridIcon /> },
  { to: '/pnl',           label: 'P&L',               icon: <PnlIcon /> },
  { to: '/orders',        label: 'Orders',           icon: <ListIcon /> },
  { to: '/broker-accounts', label: 'Accounts',        icon: <LinkIcon /> },
  { to: '/api-keys',      label: 'API Keys',         icon: <KeyIcon /> },
  { to: '/webhook-setup', label: 'Webhook Setup',    icon: <WebhookIcon /> },
  { to: '/billing',       label: 'Billing',          icon: <CreditIcon /> },
]

const ADMIN_ITEMS = [
  { to: '/admin/tenants', label: 'Tenants',    icon: <UsersIcon /> },
  { to: '/admin/stats',   label: 'Stats',      icon: <ChartIcon /> },
]

export default function AppLayout() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const [loggingOut, setLoggingOut] = useState(false)

  const handleLogout = async () => {
    setLoggingOut(true)
    await logout()
    navigate('/login')
  }

  return (
    <div className="flex h-screen bg-base-950 overflow-hidden">
      {/* Sidebar */}
      <aside className="w-56 flex-shrink-0 flex flex-col border-r border-base-800">
        {/* Wordmark */}
        <div className="h-14 flex items-center px-5 border-b border-base-800">
          <span className="font-display font-bold text-base-50 tracking-tight">
            relay<span className="text-accent">.</span>
          </span>
        </div>

        {/* Nav */}
        <nav className="flex-1 overflow-y-auto p-3 space-y-0.5">
          {NAV_ITEMS.map(item => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => clsx('nav-item', isActive && 'active')}
            >
              <span className="w-4 h-4 flex-shrink-0">{item.icon}</span>
              {item.label}
            </NavLink>
          ))}

          {user?.is_admin && (
            <>
              <div className="pt-4 pb-1 px-3">
                <span className="text-[10px] font-medium text-base-500 uppercase tracking-widest">
                  Admin
                </span>
              </div>
              {ADMIN_ITEMS.map(item => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) => clsx('nav-item', isActive && 'active')}
                >
                  <span className="w-4 h-4 flex-shrink-0">{item.icon}</span>
                  {item.label}
                </NavLink>
              ))}
            </>
          )}
        </nav>

        {/* User footer */}
        <div className="border-t border-base-800 p-3">
          <div className="flex items-center gap-3 px-2 py-2 rounded-md">
            <div className="w-7 h-7 rounded-full bg-accent/20 flex items-center justify-center flex-shrink-0">
              <span className="text-accent text-xs font-bold font-mono">
                {user?.email?.[0]?.toUpperCase() ?? '?'}
              </span>
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-xs text-base-200 truncate">{user?.email}</div>
              <div className="text-[10px] text-base-500 font-mono">{user?.plan_name ?? 'free'}</div>
            </div>
          </div>
          <button
            onClick={handleLogout}
            disabled={loggingOut}
            className="nav-item w-full mt-1 text-base-400 hover:text-loss"
          >
            <span className="w-4 h-4"><LogoutIcon /></span>
            {loggingOut ? 'Logging out…' : 'Log out'}
          </button>
          <div className="flex gap-3 mt-2 px-2">
            <a href="/privacy" className="text-[10px] text-base-600 hover:text-base-400">Privacy</a>
            <a href="/terms" className="text-[10px] text-base-600 hover:text-base-400">Terms</a>
          </div>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-6xl mx-auto px-6 py-8 animate-fade-in">
          <Outlet />
        </div>
      </main>
    </div>
  )
}

// ── Inline SVG icons (no dependency) ──────────────────────────────────────────

function GridIcon() {
  return <svg viewBox="0 0 16 16" fill="currentColor"><rect x="1" y="1" width="6" height="6" rx="1"/><rect x="9" y="1" width="6" height="6" rx="1"/><rect x="1" y="9" width="6" height="6" rx="1"/><rect x="9" y="9" width="6" height="6" rx="1"/></svg>
}
function ListIcon() {
  return <svg viewBox="0 0 16 16" fill="currentColor"><rect x="1" y="2" width="14" height="2" rx="1"/><rect x="1" y="7" width="14" height="2" rx="1"/><rect x="1" y="12" width="14" height="2" rx="1"/></svg>
}
function LinkIcon() {
  return <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M6.5 9.5a3.5 3.5 0 0 0 4.95 0l2-2a3.5 3.5 0 0 0-4.95-4.95l-1 1"/><path d="M9.5 6.5a3.5 3.5 0 0 0-4.95 0l-2 2a3.5 3.5 0 0 0 4.95 4.95l1-1"/></svg>
}
function KeyIcon() {
  return <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><circle cx="6" cy="7" r="3.5"/><path d="m9 9 5 5"/><path d="m12 12 1.5-1.5"/></svg>
}
function WebhookIcon() {
  return <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M6 3a3 3 0 1 1 4 2.83V9l3 2-3 1.5L7 11l-3 1.5L1 11l3-2V5.83A3 3 0 0 1 6 3Z"/></svg>
}
function CreditIcon() {
  return <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><rect x="1" y="3" width="14" height="10" rx="2"/><path d="M1 7h14"/></svg>
}
function UsersIcon() {
  return <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><circle cx="6" cy="5" r="2.5"/><path d="M1 13c0-2.76 2.24-5 5-5"/><circle cx="12" cy="5" r="2"/><path d="M10 13c0-2.21 1.79-4 4-4"/></svg>
}
function ChartIcon() {
  return <svg viewBox="0 0 16 16" fill="currentColor"><rect x="1" y="9" width="3" height="5" rx="0.5"/><rect x="6" y="5" width="3" height="9" rx="0.5"/><rect x="11" y="2" width="3" height="12" rx="0.5"/></svg>
}
function PnlIcon() {
  return <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M2 12l3-4 3 2 4-6"/><path d="M10 4h4v4"/></svg>
}
function LogoutIcon() {
  return <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M10 3h3a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1h-3"/><path d="m7 11 3-3-3-3"/><path d="M10 8H2"/></svg>
}
