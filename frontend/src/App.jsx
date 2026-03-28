import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { AuthProvider } from './lib/auth-context'
import { RequireAuth, RequireAdmin, RequireGuest } from './components/guards'
import AppLayout from './layouts/AppLayout'
import AuthLayout from './layouts/AuthLayout'
import LoginPage from './pages/Login'
import RegisterPage from './pages/Register'
import DashboardPage from './pages/Dashboard'
import OrdersPage from './pages/Orders'
import BrokerAccountsPage from './pages/BrokerAccounts'
import ApiKeysPage from './pages/ApiKeys'
import WebhookSetupPage from './pages/WebhookSetup'
import BillingPage from './pages/Billing'
import AdminTenantsPage from './pages/AdminTenants'
import AdminStatsPage from './pages/AdminStats'
import DailyPnlPage from './pages/DailyPnl'

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          {/* Public auth routes */}
          <Route element={<RequireGuest><AuthLayout /></RequireGuest>}>
            <Route path="/login"    element={<LoginPage />} />
            <Route path="/register" element={<RegisterPage />} />
          </Route>

          {/* Protected app routes */}
          <Route element={<RequireAuth><AppLayout /></RequireAuth>}>
            <Route path="/dashboard"       element={<DashboardPage />} />
            <Route path="/daily-pnl"       element={<DailyPnlPage />} />
            <Route path="/orders"          element={<OrdersPage />} />
            <Route path="/broker-accounts" element={<BrokerAccountsPage />} />
            <Route path="/api-keys"        element={<ApiKeysPage />} />
            <Route path="/webhook-setup"   element={<WebhookSetupPage />} />
            <Route path="/billing"         element={<BillingPage />} />

            {/* Admin-only routes */}
            <Route path="/admin/tenants" element={<RequireAdmin><AdminTenantsPage /></RequireAdmin>} />
            <Route path="/admin/stats"   element={<RequireAdmin><AdminStatsPage /></RequireAdmin>} />
          </Route>

          {/* Root redirect */}
          <Route path="/"  element={<Navigate to="/dashboard" replace />} />
          <Route path="*"  element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  )
}
