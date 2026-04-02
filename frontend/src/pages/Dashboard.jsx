import { useAuth } from '../lib/auth-context'
import { positions, orders, billing, pnl } from '../lib/api'
import { useEventSource } from '../hooks/useEventSource'
import { usePolling, useApi } from '../hooks/useApi'
import {
  PageSpinner, StatCard, PnlValue, StatusBadge,
  Mono, EmptyState, SectionHeader, brokerLabel, BrokerIcon
} from '../components/ui'
import { clsx } from 'clsx'
import PnlCharts from '../components/PnlCharts'

export default function DashboardPage() {
  const { user } = useAuth()
  const { data: pos,  loading: posLoading  } = usePolling(() => positions.list(), 30_000)
  const { data: deliveries, refetch: refetchDel } = usePolling(() => orders.deliveries({ limit: 8 }), 30_000)
  useEventSource('/api/events', 'delivery', () => refetchDel())
  const { data: sub }                         = useApi(() => billing.subscription())
  const { data: pnlDash }                     = usePolling(() => pnl.dashboard(), 15_000)

  const openPos      = (pos || []).filter(p => Math.abs(p.quantity) > 1e-9)
  const dailyPnl     = pnlDash?.daily_realized ?? 0
  const totalPnl     = pnlDash?.total_realized ?? 0
  const totalUnrealized = pnlDash?.total_unrealized ?? 0
  const hasLivePnl   = totalUnrealized !== 0 || openPos.some(p => p.unrealized_pnl != null)

  return (
    <div className="space-y-8 animate-fade-in">
      <SectionHeader
        title={`Good morning${user?.email ? `, ${user.email.split('@')[0]}` : ''}`}
        description="Live positions and recent activity"
      />

      {/* Stat row */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
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
          label="Unrealized P&L"
          value={hasLivePnl ? <PnlValue value={totalUnrealized} prefix="$" /> : '—'}
          sub={hasLivePnl ? "Live from broker" : "Polling not started"}
          accent={totalUnrealized > 0}
        />
        <StatCard
          label="Open positions"
          value={pnlDash ? pnlDash.open_positions : (posLoading ? '—' : openPos.length)}
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

      {/* Open Positions — full width */}
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
          <div className="flex flex-col items-center justify-center py-8 text-center">
            <div className="text-base-500 text-2xl mb-2">📭</div>
            <div className="text-base-300 text-sm">No open positions</div>
          </div>
        ) : (
          <>
            <div className="divide-y divide-base-800">
              {openPos.map(p => <PositionRow key={p.id} pos={p} />)}
            </div>
            {/* Totals footer */}
            <div className="px-5 py-3 border-t border-base-700 bg-base-800/30 flex items-center justify-between">
              <span className="text-xs text-base-400">Total realized P&L</span>
              <div className="flex gap-6">
                <div className="text-right">
                  <div className="text-[10px] text-base-500 mb-0.5">Today</div>
                  <PnlValue value={dailyPnl} prefix="$" decimals={2} />
                </div>
                <div className="text-right">
                  <div className="text-[10px] text-base-500 mb-0.5">All time</div>
                  <PnlValue value={totalPnl} prefix="$" decimals={2} />
                </div>
              </div>
            </div>
          </>
        )}
      </section>

      {/* P&L Charts */}
      <div className="space-y-4">
        <SectionHeader
          title="P&L by Account"
          description="Realized and unrealized profit/loss per broker account"
        />
        <PnlCharts />
      </div>

      {/* Recent Relays — full width */}
      <section className="panel overflow-hidden">
        <div className="px-5 py-4 border-b border-base-800 flex items-center justify-between">
          <h2 className="font-display font-semibold text-base-100">Recent Relays</h2>
          <a href="/webhook-setup" className="text-xs text-accent hover:text-accent-dim transition-colors">
            View all →
          </a>
        </div>
        {!deliveries ? (
          <div className="flex justify-center py-10"><PageSpinner /></div>
        ) : deliveries.length === 0 ? (
          <EmptyState icon="📡" title="No deliveries yet" />
        ) : (
          <div className="divide-y divide-base-800">
            {deliveries.map(d => <DeliveryRow key={d.id} d={d} />)}
          </div>
        )}
      </section>
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
  const isLong   = pos.quantity > 0
  const absQty   = Math.abs(pos.quantity)
  const isFuture = pos.instrument_type === 'future'
  const mult     = pos.multiplier || 1.0
  const priceDp  = pos.instrument_type === 'forex' ? 5 : 2

  return (
    <div className="px-5 py-3.5 hover:bg-base-800/40 transition-colors">
      {/* Top row: direction, symbol, qty */}
      <div className="flex items-center gap-4">
        <div className={clsx(
          'text-xs font-mono font-semibold px-1.5 py-0.5 rounded flex-shrink-0',
          isLong ? 'bg-accent/10 text-accent' : 'bg-loss/10 text-loss'
        )}>
          {isLong ? 'LONG' : 'SHORT'}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-mono text-base-100">{pos.symbol}</span>
            {isFuture && (
              <span className="text-[10px] font-mono text-base-500 bg-base-700 px-1 rounded">
                ×{mult}
              </span>
            )}
          </div>
          <div className="flex items-center gap-1 text-xs text-base-400 mt-0.5">
            <BrokerIcon broker={pos.broker} size={12} />
            {brokerLabel(pos.broker)} · {pos.account}
          </div>
        </div>
        <div className="text-right">
          <Mono className="text-base-100">{absQty.toLocaleString()}</Mono>
          <div className="text-[10px] text-base-500 mt-0.5">
            avg <span className="font-mono text-base-400">
              {pos.avg_price ? pos.avg_price.toFixed(priceDp) : '—'}
            </span>
          </div>
        </div>
      </div>

      {/* P&L row */}
      <div className="flex items-center justify-between mt-2.5 pt-2.5 border-t border-base-800/60">
        <div className="flex gap-5">
          <div>
            <div className="text-[10px] text-base-500 mb-0.5">Today</div>
            <PnlValue value={pos.daily_realized_pnl || 0} prefix="$" decimals={2} />
          </div>
          <div>
            <div className="text-[10px] text-base-500 mb-0.5">Realized</div>
            <PnlValue value={pos.realized_pnl || 0} prefix="$" decimals={2} />
          </div>
          {pos.unrealized_pnl != null && (
            <div>
              <div className="text-[10px] text-base-500 mb-0.5 flex items-center gap-1">
                Unrealized
                <span className="w-1.5 h-1.5 rounded-full bg-accent inline-block animate-pulse-slow" />
              </div>
              <PnlValue value={pos.unrealized_pnl} prefix="$" decimals={2} />
            </div>
          )}
        </div>
        <div className="text-right">
          {pos.last_price != null && (
            <div className="text-[10px] font-mono text-base-400 mb-0.5">
              last <span className="text-base-300">{pos.last_price.toFixed(priceDp)}</span>
            </div>
          )}
          {pos.last_price_at && (
            <div className="text-[10px] text-base-600 font-mono">
              {secondsAgo(pos.last_price_at)}s ago
            </div>
          )}
          {pos.unrealized_pnl == null && (
            <div className="text-[10px] text-base-600 font-mono">
              {isFuture ? `pts × ${absQty} × $${mult}` : 'awaiting poll'}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function secondsAgo(isoString) {
  const diff = Math.floor((Date.now() - new Date(isoString).getTime()) / 1000)
  return diff < 0 ? 0 : diff
}

function DeliveryRow({ d }) {
  const target = (() => {
    try {
      const p = JSON.parse(d.raw_payload || '{}')
      return { action: (p.action || '').toUpperCase(), sym: p.symbol || '', acct: p.account || 'primary', broker: p.broker || '' }
    } catch { return null }
  })()

  return (
    <div className="px-5 py-3 flex items-center gap-4 hover:bg-base-800/40 transition-colors">
      <span className="font-mono text-xs text-base-400 w-20 flex-shrink-0">
        {new Date(d.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
      </span>
      <StatusBadge status={d.outcome} />
      {target && (
        <span className="font-mono text-xs text-base-300 truncate flex-shrink-0" style={{maxWidth: '20rem'}}>
          <span className={target.action === 'BUY' ? 'text-accent' : target.action === 'SELL' ? 'text-loss' : 'text-warn'}>{target.action}</span>
          {' '}{target.sym}
          <span className="text-base-500">{' '}TradingView → </span>
          {target.broker && <BrokerIcon broker={target.broker} size={12} className="mx-0.5" />}
          {d.account_display_name || target.acct}
        </span>
      )}
      {d.algo && (
        <span className="font-mono text-[10px] text-base-400 bg-base-800 px-1.5 py-0.5 rounded flex-shrink-0">
          {d.algo}
        </span>
      )}
      <span className="font-mono text-xs text-base-500 flex-shrink-0 text-right" title={d.broker_latency_ms ? `Processing: ${Math.max(0, d.duration_ms - d.broker_latency_ms).toFixed(0)}ms | Broker: ${d.broker_latency_ms.toFixed(0)}ms` : ''}>
        {d.duration_ms ? (
          d.broker_latency_ms ? (
            <>{Math.max(0, d.duration_ms - d.broker_latency_ms).toFixed(0)}<span className="text-base-600">+</span>{d.broker_latency_ms.toFixed(0)}<span className="text-base-600">ms</span></>
          ) : `${d.duration_ms.toFixed(0)}ms`
        ) : '—'}
      </span>
      <span className="font-mono text-xs text-base-500 flex-1">{d.source_ip || '—'}</span>
      {d.error_detail && (
        <span className="text-xs text-loss font-mono truncate max-w-[12rem] flex-shrink-0">
          {d.error_detail.slice(0, 50)}{d.error_detail.length > 50 ? '…' : ''}
        </span>
      )}
      <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded flex-shrink-0 ${
        d.auth_passed ? 'bg-accent/10 text-accent' : 'bg-loss/10 text-loss'
      }`}>
        {d.auth_passed ? 'ok' : 'fail'}
      </span>
    </div>
  )
}
