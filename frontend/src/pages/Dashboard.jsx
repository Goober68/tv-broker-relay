import { useAuth } from '../lib/auth-context'
import { positions, orders, billing } from '../lib/api'
import { usePolling, useApi } from '../hooks/useApi'
import {
  PageSpinner, StatCard, PnlValue, StatusBadge,
  Mono, EmptyState, SectionHeader
} from '../components/ui'
import { clsx } from 'clsx'

export default function DashboardPage() {
  const { user } = useAuth()
  const { data: pos,  loading: posLoading  } = usePolling(() => positions.list(), 30_000)
  const { data: recent }                      = usePolling(() => orders.list({ limit: 8 }), 30_000)
  const { data: sub }                         = useApi(() => billing.subscription())

  const openPos    = (pos || []).filter(p => Math.abs(p.quantity) > 1e-9)
  const dailyPnl   = openPos.reduce((sum, p) => sum + (p.daily_realized_pnl || 0), 0)
  const totalPnl   = openPos.reduce((sum, p) => sum + (p.realized_pnl || 0), 0)

  return (
    <div className="space-y-8 animate-fade-in">
      <SectionHeader
        title={`Good morning${user?.email ? `, ${user.email.split('@')[0]}` : ''}`}
        description="Live positions and recent activity"
      />

      {/* Stat row */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <StatCard
          label="Daily P&L"
          value={<PnlValue value={dailyPnl} prefix="$" />}
          sub="Today's realized"
          accent={dailyPnl > 0}
        />
        <StatCard
          label="Total P&L"
          value={<PnlValue value={totalPnl} prefix="$" />}
          sub="All time realized"
        />
        <StatCard
          label="Open positions"
          value={posLoading ? '—' : openPos.length}
          sub="Active across all brokers"
        />
        <StatCard
          label="Orders this period"
          value={sub ? `${sub.orders_this_period}${sub.orders_remaining != null ? ` / ${sub.orders_this_period + sub.orders_remaining}` : ''}` : '—'}
          sub={`${sub?.plan?.display_name || '—'} plan`}
        />
      </div>

      {/* Plan usage bar */}
      {sub && sub.orders_remaining != null && (
        <UsageBar used={sub.orders_this_period} total={sub.orders_this_period + sub.orders_remaining} />
      )}

      <div className="grid lg:grid-cols-2 gap-6">
        {/* Open Positions */}
        <section className="panel overflow-hidden">
          <div className="px-5 py-4 border-b border-base-800 flex items-center justify-between">
            <h2 className="font-display font-semibold text-base-100">Open Positions</h2>
            <span className="text-xs text-base-400 font-mono">
              {posLoading ? '…' : `${openPos.length} active`}
            </span>
          </div>
          {posLoading ? (
            <div className="flex justify-center py-10"><PageSpinner /></div>
          ) : openPos.length === 0 ? (
            <EmptyState
              icon="📭"
              title="No open positions"
              description="Submit a webhook alert to open your first position."
            />
          ) : (
            <div className="divide-y divide-base-800">
              {openPos.map(p => <PositionRow key={p.id} pos={p} />)}
            </div>
          )}
        </section>

        {/* Recent Orders */}
        <section className="panel overflow-hidden">
          <div className="px-5 py-4 border-b border-base-800 flex items-center justify-between">
            <h2 className="font-display font-semibold text-base-100">Recent Orders</h2>
            <a href="/orders" className="text-xs text-accent hover:text-accent-dim transition-colors">
              View all →
            </a>
          </div>
          {!recent ? (
            <div className="flex justify-center py-10"><PageSpinner /></div>
          ) : recent.length === 0 ? (
            <EmptyState icon="📋" title="No orders yet" />
          ) : (
            <div className="divide-y divide-base-800">
              {recent.map(o => <OrderRow key={o.id} order={o} />)}
            </div>
          )}
        </section>
      </div>
    </div>
  )
}

function UsageBar({ used, total }) {
  const pct = total > 0 ? Math.min((used / total) * 100, 100) : 0
  const isHigh = pct > 80
  return (
    <div className="panel p-4">
      <div className="flex items-center justify-between text-xs text-base-400 mb-2">
        <span>Monthly order usage</span>
        <span className="font-mono">{used} / {total}</span>
      </div>
      <div className="h-1.5 bg-base-700 rounded-full overflow-hidden">
        <div
          className={clsx(
            'h-full rounded-full transition-all duration-500',
            isHigh ? 'bg-warn' : 'bg-accent'
          )}
          style={{ width: `${pct}%` }}
        />
      </div>
      {isHigh && (
        <p className="text-xs text-warn mt-2">
          You've used {pct.toFixed(0)}% of your monthly limit.{' '}
          <a href="/billing" className="underline hover:text-warn-dim">Upgrade</a> to avoid interruption.
        </p>
      )}
    </div>
  )
}

function PositionRow({ pos }) {
  const isLong  = pos.quantity > 0
  const absQty  = Math.abs(pos.quantity)
  return (
    <div className="px-5 py-3.5 flex items-center gap-4 hover:bg-base-800/40 transition-colors">
      <div className={clsx(
        'text-xs font-mono font-semibold px-1.5 py-0.5 rounded',
        isLong ? 'bg-accent/10 text-accent' : 'bg-loss/10 text-loss'
      )}>
        {isLong ? 'LONG' : 'SHORT'}
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-sm font-mono text-base-100">{pos.symbol}</div>
        <div className="text-xs text-base-400">{pos.broker} · {pos.account}</div>
      </div>
      <div className="text-right">
        <Mono className="text-base-100">{absQty.toLocaleString()}</Mono>
        <div className="text-xs">
          <PnlValue value={pos.daily_realized_pnl} />
        </div>
      </div>
    </div>
  )
}

function OrderRow({ order }) {
  const isBuy = order.action === 'buy'
  return (
    <div className="px-5 py-3 flex items-center gap-3 hover:bg-base-800/40 transition-colors">
      <div className={clsx(
        'text-[10px] font-mono font-bold px-1.5 py-0.5 rounded w-8 text-center',
        isBuy ? 'bg-accent/10 text-accent' : 'bg-loss/10 text-loss'
      )}>
        {order.action.toUpperCase()}
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-sm font-mono text-base-100">{order.symbol}</div>
        <div className="text-xs text-base-400">
          {order.broker} · {new Date(order.created_at).toLocaleTimeString()}
        </div>
      </div>
      <div className="text-right flex items-center gap-2">
        <Mono className="text-base-400 text-xs">{order.quantity.toLocaleString()}</Mono>
        <StatusBadge status={order.status} />
      </div>
    </div>
  )
}
