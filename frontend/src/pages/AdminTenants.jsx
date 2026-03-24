import { useState } from 'react'
import { admin as adminApi } from '../lib/api'
import { useApi } from '../hooks/useApi'
import { PageSpinner, SectionHeader, StatusBadge, Alert, EmptyState, Spinner } from '../components/ui'

const PLANS = ['free', 'pro', 'enterprise']

export default function AdminTenantsPage() {
  const { data, loading, error, refetch } = useApi(() => adminApi.tenants({ limit: 200 }))
  const [search, setSearch]   = useState('')
  const [planFilter, setPlan] = useState('')
  const [actionId, setActionId] = useState(null)
  const [actionErr, setActionErr] = useState(null)

  const filtered = (data || []).filter(t => {
    const matchSearch = !search || t.email.toLowerCase().includes(search.toLowerCase())
    const matchPlan   = !planFilter || t.plan_name === planFilter
    return matchSearch && matchPlan
  })

  const handleAssignPlan = async (tenantId, planName) => {
    setActionId(tenantId)
    setActionErr(null)
    try {
      await adminApi.assignPlan(tenantId, planName)
      refetch()
    } catch (err) {
      setActionErr(err.detail || 'Failed')
    } finally {
      setActionId(null)
    }
  }

  const handleToggleActive = async (tenantId, isActive) => {
    setActionId(tenantId)
    try {
      await adminApi.setActive(tenantId, !isActive)
      refetch()
    } catch {}
    finally { setActionId(null) }
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <SectionHeader
        title="Tenants"
        description={`${data?.length ?? '…'} total registered`}
        action={<button onClick={refetch} className="btn-ghost text-xs">↻ Refresh</button>}
      />

      <Alert type="error" message={actionErr} />

      {/* Filters */}
      <div className="flex flex-wrap gap-3 panel p-4">
        <input
          className="input flex-1 min-w-48 py-1.5"
          placeholder="Search by email…"
          value={search}
          onChange={e => setSearch(e.target.value)}
        />
        <select className="input w-40 py-1.5" value={planFilter} onChange={e => setPlan(e.target.value)}>
          <option value="">All plans</option>
          {PLANS.map(p => <option key={p} value={p}>{p}</option>)}
        </select>
      </div>

      {/* Table */}
      <div className="panel overflow-hidden">
        {loading ? (
          <div className="flex justify-center py-16"><PageSpinner /></div>
        ) : error ? (
          <Alert type="error" message={error} className="m-5" />
        ) : filtered.length === 0 ? (
          <EmptyState icon="👥" title="No tenants found" />
        ) : (
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Email</th>
                  <th>Plan</th>
                  <th>Status</th>
                  <th>Orders this period</th>
                  <th>Admin</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map(tenant => (
                  <tr key={tenant.id} className={!tenant.is_active ? 'opacity-50' : ''}>
                    <td>
                      <div className="text-sm text-base-100">{tenant.email}</div>
                      <div className="text-xs text-base-500 font-mono">#{tenant.id}</div>
                    </td>
                    <td>
                      <PlanSelector
                        current={tenant.plan_name}
                        loading={actionId === tenant.id}
                        onSelect={plan => handleAssignPlan(tenant.id, plan)}
                      />
                    </td>
                    <td>
                      <StatusBadge status={tenant.subscription_status || 'none'} />
                    </td>
                    <td>
                      <span className="font-mono text-sm">{tenant.orders_this_period}</span>
                    </td>
                    <td>
                      {tenant.is_admin && <span className="badge badge-amber">admin</span>}
                    </td>
                    <td>
                      <button
                        onClick={() => handleToggleActive(tenant.id, tenant.is_active)}
                        disabled={actionId === tenant.id}
                        className={`text-xs transition-colors ${tenant.is_active ? 'text-base-500 hover:text-loss' : 'text-base-500 hover:text-accent'}`}
                      >
                        {actionId === tenant.id
                          ? <Spinner size="sm" />
                          : tenant.is_active ? 'Disable' : 'Enable'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <p className="text-xs text-base-500 text-right">
        Showing {filtered.length} of {data?.length ?? 0} tenants
      </p>
    </div>
  )
}

function PlanSelector({ current, loading, onSelect }) {
  return (
    <div className="flex items-center gap-2">
      {loading ? (
        <Spinner size="sm" />
      ) : (
        <select
          className="bg-transparent border-0 text-sm font-mono text-base-200 focus:outline-none cursor-pointer hover:text-base-50"
          value={current || ''}
          onChange={e => onSelect(e.target.value)}
        >
          {PLANS.map(p => (
            <option key={p} value={p} className="bg-base-800">{p}</option>
          ))}
        </select>
      )}
    </div>
  )
}
