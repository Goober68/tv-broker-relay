import { useState } from 'react'
import { billing as billingApi } from '../lib/api'
import { useApi } from '../hooks/useApi'
import { PageSpinner, SectionHeader, Alert, StatusBadge, Spinner } from '../components/ui'
import { clsx } from 'clsx'

export default function BillingPage() {
  const { data: sub, loading: subLoading, refetch } = useApi(() => billingApi.subscription())
  const { data: plans, loading: plansLoading }      = useApi(() => billingApi.plans())
  const [upgrading, setUpgrading] = useState(null)
  const [portalLoading, setPortalLoading] = useState(false)
  const [error, setError] = useState(null)

  const handleUpgrade = async (plan_name) => {
    setUpgrading(plan_name)
    setError(null)
    try {
      const { url } = await billingApi.checkout(plan_name)
      window.location.href = url
    } catch (err) {
      setError(err.detail || 'Failed to start checkout')
      setUpgrading(null)
    }
  }

  const handlePortal = async () => {
    setPortalLoading(true)
    setError(null)
    try {
      const { url } = await billingApi.portal()
      window.location.href = url
    } catch (err) {
      setError(err.detail || 'Failed to open billing portal')
    } finally {
      setPortalLoading(false)
    }
  }

  if (subLoading || plansLoading) return <PageSpinner />

  const currentPlan = sub?.plan?.name
  const limit       = sub?.plan?.max_monthly_orders
  const used        = sub?.orders_this_period || 0
  const remaining   = sub?.orders_remaining
  const pct         = limit && limit > 0 && limit !== -1
    ? Math.min((used / (used + (remaining || 0))) * 100, 100) : 0

  return (
    <div className="space-y-8 animate-fade-in">
      <SectionHeader title="Billing" description="Manage your plan and payment method" />

      <Alert type="error" message={error} />

      {/* Current plan */}
      {sub && (
        <div className="panel p-6 space-y-5">
          <div className="flex items-start justify-between">
            <div>
              <div className="text-xs text-base-400 uppercase tracking-wider mb-1">Current plan</div>
              <div className="font-display font-bold text-2xl text-base-50">
                {sub.plan.display_name}
                <StatusBadge status={sub.status} />
              </div>
            </div>
            {sub.stripe_customer_id && (
              <button
                onClick={handlePortal}
                disabled={portalLoading}
                className="btn-ghost flex items-center gap-2"
              >
                {portalLoading && <Spinner size="sm" />}
                Manage subscription →
              </button>
            )}
          </div>

          {/* Usage */}
          <div>
            <div className="flex justify-between text-xs text-base-400 mb-2">
              <span>Monthly orders</span>
              <span className="font-mono">
                {used}{limit === -1 ? ' / ∞' : ` / ${used + (remaining || 0)}`}
              </span>
            </div>
            {limit !== -1 && (
              <div className="h-1.5 bg-base-700 rounded-full overflow-hidden">
                <div
                  className={clsx('h-full rounded-full transition-all', pct > 80 ? 'bg-warn' : 'bg-accent')}
                  style={{ width: `${pct}%` }}
                />
              </div>
            )}
          </div>

          {/* Plan limits */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <LimitCell label="Brokers"     value={sub.plan.max_broker_accounts === -1 ? '∞' : sub.plan.max_broker_accounts} />
            <LimitCell label="Orders/mo"   value={sub.plan.max_monthly_orders === -1 ? '∞' : sub.plan.max_monthly_orders} />
            <LimitCell label="Open orders" value={sub.plan.max_open_orders === -1 ? '∞' : sub.plan.max_open_orders} />
            <LimitCell label="Rate limit"  value={`${sub.plan.requests_per_minute}/min`} />
          </div>

          {sub.plan.allowed_order_types && (
            <p className="text-xs text-base-400">
              Order types: <span className="font-mono text-base-300">{sub.plan.allowed_order_types.join(', ')}</span>
            </p>
          )}
        </div>
      )}

      {/* Upgrade cards */}
      {plans && (
        <div>
          <h2 className="font-display font-semibold text-base-200 mb-4">Available plans</h2>
          <div className="grid md:grid-cols-3 gap-4">
            {plans.map(plan => (
              <PlanCard
                key={plan.name}
                plan={plan}
                isCurrent={plan.name === currentPlan}
                onUpgrade={() => handleUpgrade(plan.name)}
                upgrading={upgrading === plan.name}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function LimitCell({ label, value }) {
  return (
    <div className="bg-base-800 rounded-md px-3 py-2.5">
      <div className="text-[10px] text-base-500 uppercase tracking-wider">{label}</div>
      <div className="font-mono font-semibold text-base-100 mt-0.5">{value}</div>
    </div>
  )
}

function PlanCard({ plan, isCurrent, onUpgrade, upgrading }) {
  const isPro  = plan.name === 'pro'
  const isFree = plan.name === 'free'

  return (
    <div className={clsx(
      'panel p-5 flex flex-col gap-4 relative',
      isPro && 'border-accent/30',
      isCurrent && 'ring-1 ring-accent/20'
    )}>
      {isPro && (
        <div className="absolute -top-2.5 left-4">
          <span className="badge badge-green text-[10px]">Most popular</span>
        </div>
      )}

      <div>
        <div className="font-display font-bold text-lg text-base-50">{plan.display_name}</div>
      </div>

      <ul className="space-y-1.5 text-sm flex-1">
        <FeatureRow label="Broker accounts" value={plan.max_broker_accounts === -1 ? 'Unlimited' : plan.max_broker_accounts} />
        <FeatureRow label="Orders / month"  value={plan.max_monthly_orders === -1 ? 'Unlimited' : plan.max_monthly_orders.toLocaleString()} />
        <FeatureRow label="Open orders"     value={plan.max_open_orders === -1 ? 'Unlimited' : plan.max_open_orders} />
        <FeatureRow label="Rate limit"      value={`${plan.requests_per_minute} / min`} />
        <FeatureRow
          label="Order types"
          value={plan.allowed_order_types ? plan.allowed_order_types.join(', ') : 'All types'}
        />
      </ul>

      {isCurrent ? (
        <div className="btn-ghost text-center text-xs opacity-60 cursor-default">Current plan</div>
      ) : isFree ? (
        <div className="btn-ghost text-center text-xs opacity-60 cursor-default">Downgrade via portal</div>
      ) : (
        <button
          onClick={onUpgrade}
          disabled={!!upgrading}
          className={clsx('w-full flex items-center justify-center gap-2', isPro ? 'btn-primary' : 'btn-ghost border border-base-600')}
        >
          {upgrading && <Spinner size="sm" />}
          {upgrading ? 'Redirecting…' : `Upgrade to ${plan.display_name}`}
        </button>
      )}
    </div>
  )
}

function FeatureRow({ label, value }) {
  return (
    <li className="flex items-center justify-between text-xs">
      <span className="text-base-400">{label}</span>
      <span className="font-mono text-base-200">{value}</span>
    </li>
  )
}
