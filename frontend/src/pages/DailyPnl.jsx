import { useState } from 'react'
import { pnl as pnlApi, positions } from '../lib/api'
import { usePolling } from '../hooks/useApi'
import { PageSpinner, SectionHeader, PnlValue, EmptyState, brokerLabel } from '../components/ui'
import { LineChart, Line, ResponsiveContainer, ReferenceLine, Tooltip, YAxis } from 'recharts'
import { clsx } from 'clsx'

const PERIODS = [
  { key: 'today',   label: 'Today',   apiPeriod: '15min', desc: 'Intraday 15-min bars', colLabel: "Today's" },
  { key: 'daily',   label: 'Daily',   apiPeriod: 'daily', desc: 'Last 30 days', colLabel: '30-Day' },
  { key: 'weekly',  label: 'Weekly',  apiPeriod: 'weekly', desc: 'Last 12 weeks', colLabel: '12-Week' },
  { key: 'monthly', label: 'Monthly', apiPeriod: 'monthly', desc: 'Last 12 months', colLabel: '12-Month' },
  { key: 'yearly',  label: 'Yearly',  apiPeriod: 'yearly', desc: 'All time', colLabel: 'All-Time' },
]

export default function PnlPage() {
  const [period, setPeriod] = useState('today')
  const current = PERIODS.find(p => p.key === period)

  const { data, loading, refetch } = usePolling(
    () => pnlApi.summary(current.apiPeriod),
    30_000,
    [period],
  )
  const { data: pos } = usePolling(() => positions.list(), 30_000)

  // Aggregate positions by broker/account for unrealized + daily realized
  const acctPositions = {}
  for (const p of pos || []) {
    const key = `${p.broker}:${p.account}`
    if (!acctPositions[key]) acctPositions[key] = { unrealized: 0, dailyRealized: 0, openCount: 0 }
    if (Math.abs(p.quantity) > 1e-9) {
      acctPositions[key].unrealized += p.unrealized_pnl || 0
      acctPositions[key].dailyRealized += p.daily_realized_pnl || 0
      acctPositions[key].openCount += 1
    }
  }

  // Grand totals
  const totals = (data || []).reduce((acc, a) => {
    const key = `${a.broker}:${a.account}`
    const ap = acctPositions[key] || { unrealized: 0, dailyRealized: 0 }
    const realized = a.bars.reduce((s, b) => s + b.realized_pnl, 0)
    acc.realized += realized
    acc.unrealized += ap.unrealized
    acc.total += realized + ap.unrealized
    acc.orders += a.bars.reduce((s, b) => s + b.order_count, 0)
    return acc
  }, { realized: 0, unrealized: 0, total: 0, orders: 0 })

  return (
    <div className="space-y-4 animate-fade-in">
      <div className="flex items-center justify-between">
        <SectionHeader
          title="P&L"
          description={current.desc}
        />
        <button onClick={refetch} className="text-xs text-base-400 hover:text-base-200">↻</button>
      </div>

      {/* Period toggle */}
      <div className="flex bg-base-800 rounded-md p-0.5 gap-0.5 w-fit">
        {PERIODS.map(p => (
          <button
            key={p.key}
            onClick={() => setPeriod(p.key)}
            className={clsx(
              'px-3 py-1.5 text-xs font-mono rounded transition-colors',
              period === p.key
                ? 'bg-base-600 text-base-50'
                : 'text-base-400 hover:text-base-200'
            )}
          >
            {p.label}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="flex justify-center py-10"><PageSpinner /></div>
      ) : !data?.length ? (
        <EmptyState icon="📊" title="No broker accounts" description="Add a broker account to see P&L." />
      ) : (
        <>
          {/* Grand total bar */}
          <div className="panel px-5 py-3 flex items-center justify-between">
            <div className="flex items-center gap-6">
              <div>
                <div className="text-[10px] text-base-500 uppercase tracking-wider">{current.colLabel} P&L</div>
                <PnlValue value={totals.total} prefix="$" decimals={2} className="text-lg font-semibold" />
              </div>
              <div>
                <div className="text-[10px] text-base-500">{current.colLabel} Realized</div>
                <PnlValue value={totals.realized} prefix="$" decimals={2} />
              </div>
              <div>
                <div className="text-[10px] text-base-500">Unrealized</div>
                <PnlValue value={totals.unrealized} prefix="$" decimals={2} />
              </div>
            </div>
            <div className="text-xs text-base-500 font-mono">{totals.orders} fills</div>
          </div>

          {/* Account table */}
          <div className="panel overflow-hidden">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-base-800 text-base-500">
                  <th className="text-left font-medium px-4 py-2.5">Account</th>
                  <th className="text-right font-medium px-3 py-2.5 w-28">{current.colLabel} Realized</th>
                  <th className="text-right font-medium px-3 py-2.5 w-24">Unrealized</th>
                  <th className="text-right font-medium px-3 py-2.5 w-28">{current.colLabel} Total</th>
                  <th className="text-right font-medium px-3 py-2.5 w-14">Fills</th>
                  <th className="text-right font-medium px-3 py-2.5 w-14">Open</th>
                  <th className="text-center font-medium px-2 py-2.5 w-44">
                    {period === 'today' ? 'Intraday' : 'Trend'}
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-base-800/60">
                {data.map(account => {
                  const key = `${account.broker}:${account.account}`
                  const ap = acctPositions[key] || { unrealized: 0, dailyRealized: 0, openCount: 0 }
                  const realized = account.bars.reduce((s, b) => s + b.realized_pnl, 0)
                  const unrealized = ap.unrealized
                  const total = realized + unrealized
                  const orderCount = account.bars.reduce((s, b) => s + b.order_count, 0)

                  return (
                    <tr key={key} className="hover:bg-base-800/30 transition-colors">
                      <td className="px-4 py-2.5">
                        <div className="font-mono text-base-100">
                          {account.display_name || account.account}
                        </div>
                        <div className="text-[10px] text-base-500 mt-0.5">{brokerLabel(account.broker)}</div>
                      </td>
                      <td className="text-right px-3 py-2.5 font-mono">
                        <PnlValue value={realized} prefix="$" decimals={2} />
                      </td>
                      <td className="text-right px-3 py-2.5 font-mono">
                        <PnlValue value={unrealized} prefix="$" decimals={2} />
                      </td>
                      <td className="text-right px-3 py-2.5 font-mono font-semibold">
                        <PnlValue value={total} prefix="$" decimals={2} />
                      </td>
                      <td className="text-right px-3 py-2.5 font-mono text-base-400">
                        {orderCount}
                      </td>
                      <td className="text-right px-3 py-2.5 font-mono text-base-400">
                        {ap.openCount}
                      </td>
                      <td className="px-2 py-2.5">
                        <Sparkline bars={account.bars} period={period} />
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}

function Sparkline({ bars, period }) {
  if (!bars.length) {
    return <div className="h-8 flex items-center justify-center text-[10px] text-base-600">—</div>
  }

  const lastVal = bars[bars.length - 1].cumulative_total
  const color = lastVal >= 0 ? '#00e5a0' : '#ff4d4d'

  const fmtLabel = (iso) => {
    const d = new Date(iso)
    if (period === 'today') return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    if (period === 'daily') return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
    if (period === 'weekly') return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
    if (period === 'monthly') return d.toLocaleDateString([], { month: 'short', year: '2-digit' })
    return d.toLocaleDateString([], { year: 'numeric' })
  }

  return (
    <ResponsiveContainer width="100%" height={32}>
      <LineChart data={bars} margin={{ top: 2, right: 2, bottom: 2, left: 2 }}>
        <YAxis domain={['dataMin', 'dataMax']} hide />
        <ReferenceLine y={0} stroke="#2e2e36" strokeWidth={1} />
        <Tooltip
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null
            const d = payload[0].payload
            return (
              <div className="bg-base-900 border border-base-700 rounded px-2 py-1 text-[10px] font-mono shadow-lg">
                <div className="text-base-400">{fmtLabel(d.period_start)}</div>
                <div className={d.cumulative_total >= 0 ? 'text-accent' : 'text-loss'}>
                  {d.cumulative_total >= 0 ? '+' : ''}{d.cumulative_total.toFixed(2)}
                </div>
              </div>
            )
          }}
        />
        <Line
          type="linear"
          dataKey="cumulative_total"
          stroke={color}
          strokeWidth={1.5}
          dot={false}
          activeDot={{ r: 2, fill: color }}
        />
      </LineChart>
    </ResponsiveContainer>
  )
}
