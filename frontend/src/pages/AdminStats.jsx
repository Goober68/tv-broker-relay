import { admin as adminApi, orders as ordersApi } from '../lib/api'
import { useApi } from '../hooks/useApi'
import { PageSpinner, SectionHeader, StatCard, Alert } from '../components/ui'
import {
  BarChart, Bar, XAxis, YAxis, Tooltip,
  ResponsiveContainer, Cell
} from 'recharts'

export default function AdminStatsPage() {
  const { data: stats,   loading: statsLoading,  error: statsErr  } = useApi(() => adminApi.stats())
  const { data: tenants, loading: tenantsLoading                   } = useApi(() => adminApi.tenants({ limit: 200 }))
  const { data: plans,   loading: plansLoading                     } = useApi(() => adminApi.plans())

  const loading = statsLoading || tenantsLoading || plansLoading

  // Derive plan distribution from tenants list
  const planCounts = {}
  if (tenants) {
    for (const t of tenants) {
      const p = t.plan_name || 'unknown'
      planCounts[p] = (planCounts[p] || 0) + 1
    }
  }
  const planChartData = Object.entries(planCounts).map(([name, count]) => ({ name, count }))

  // Active vs inactive
  const activeCount   = tenants?.filter(t => t.is_active).length ?? 0
  const inactiveCount = tenants?.filter(t => !t.is_active).length ?? 0
  const adminCount    = tenants?.filter(t => t.is_admin).length ?? 0

  // Total orders this period across all tenants
  const totalOrders = tenants?.reduce((sum, t) => sum + (t.orders_this_period || 0), 0) ?? 0

  if (loading) return <PageSpinner />

  return (
    <div className="space-y-8 animate-fade-in">
      <SectionHeader title="Platform Stats" description="Overview across all tenants" />

      <Alert type="error" message={statsErr} />

      {/* Top stats */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <StatCard
          label="Total tenants"
          value={tenants?.length ?? '—'}
          sub={`${activeCount} active · ${inactiveCount} disabled`}
          accent
        />
        <StatCard
          label="Admin users"
          value={adminCount}
          sub="Can access /admin"
        />
        <StatCard
          label="Orders this period"
          value={totalOrders.toLocaleString()}
          sub="Across all tenants"
        />
        <StatCard
          label="Plans configured"
          value={plans?.filter(p => p.stripe_price_id).length ?? 0}
          sub={`of ${plans?.length ?? 0} have Stripe price IDs`}
        />
      </div>

      <div className="grid lg:grid-cols-2 gap-6">
        {/* Plan distribution chart */}
        <section className="panel p-6">
          <h2 className="font-display font-semibold text-base-100 mb-5">Tenants by plan</h2>
          {planChartData.length === 0 ? (
            <div className="text-base-500 text-sm text-center py-8">No data</div>
          ) : (
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={planChartData} margin={{ top: 4, right: 8, bottom: 4, left: 0 }}>
                <XAxis
                  dataKey="name"
                  tick={{ fill: '#7e7e90', fontSize: 11, fontFamily: 'JetBrains Mono' }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  tick={{ fill: '#7e7e90', fontSize: 11, fontFamily: 'JetBrains Mono' }}
                  axisLine={false}
                  tickLine={false}
                  allowDecimals={false}
                />
                <Tooltip
                  contentStyle={{
                    backgroundColor: '#18181d',
                    border: '1px solid #2e2e36',
                    borderRadius: 6,
                    fontSize: 12,
                    fontFamily: 'JetBrains Mono',
                    color: '#d4d4e0',
                  }}
                  cursor={{ fill: 'rgba(255,255,255,0.03)' }}
                />
                <Bar dataKey="count" radius={[3, 3, 0, 0]}>
                  {planChartData.map((entry, i) => (
                    <Cell
                      key={i}
                      fill={
                        entry.name === 'enterprise' ? '#00e5a0' :
                        entry.name === 'pro'        ? '#00b87f' :
                                                      '#2e2e36'
                      }
                    />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          )}
        </section>

        {/* Plans table — with Stripe price IDs */}
        <section className="panel overflow-hidden">
          <div className="px-5 py-4 border-b border-base-800">
            <h2 className="font-display font-semibold text-base-100">Plan configuration</h2>
          </div>
          <table className="data-table">
            <thead>
              <tr>
                <th>Plan</th>
                <th>Orders/mo</th>
                <th>Stripe price ID</th>
              </tr>
            </thead>
            <tbody>
              {(plans || []).map(plan => (
                <tr key={plan.name}>
                  <td className="font-mono font-medium text-base-100">{plan.name}</td>
                  <td className="font-mono text-base-400">
                    {plan.max_monthly_orders === -1 ? '∞' : plan.max_monthly_orders}
                  </td>
                  <td>
                    {plan.stripe_price_id ? (
                      <code className="font-mono text-xs text-accent">{plan.stripe_price_id}</code>
                    ) : (
                      <span className="text-xs text-warn italic">not set</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>

      {/* Top tenants by order volume */}
      <section className="panel overflow-hidden">
        <div className="px-5 py-4 border-b border-base-800">
          <h2 className="font-display font-semibold text-base-100">Top tenants by volume</h2>
        </div>
        <table className="data-table">
          <thead>
            <tr>
              <th>Email</th>
              <th>Plan</th>
              <th>Orders this period</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {(tenants || [])
              .filter(t => t.orders_this_period > 0)
              .sort((a, b) => b.orders_this_period - a.orders_this_period)
              .slice(0, 10)
              .map(t => (
                <tr key={t.id}>
                  <td className="text-sm text-base-100">{t.email}</td>
                  <td><span className="font-mono text-xs text-base-400">{t.plan_name}</span></td>
                  <td><span className="font-mono">{t.orders_this_period}</span></td>
                  <td>
                    <span className={`badge ${t.is_active ? 'badge-green' : 'badge-neutral'}`}>
                      {t.is_active ? 'active' : 'disabled'}
                    </span>
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      </section>
    </div>
  )
}
