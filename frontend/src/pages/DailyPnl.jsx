import { pnl as pnlApi, positions } from '../lib/api'
import { usePolling } from '../hooks/useApi'
import { PageSpinner, SectionHeader, PnlValue, EmptyState, brokerLabel } from '../components/ui'
import { LineChart, Line, ResponsiveContainer, ReferenceLine, Tooltip, YAxis } from 'recharts'

export default function DailyPnlPage() {
  const { data, loading, refetch } = usePolling(
    () => pnlApi.summary('15min'),
    30_000,
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
          title="Today's P&L"
          description={new Date().toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' })}
        />
        <button onClick={refetch} className="text-xs text-base-400 hover:text-base-200">↻</button>
      </div>

      {loading ? (
        <div className="flex justify-center py-10"><PageSpinner /></div>
      ) : !data?.length ? (
        <EmptyState icon="📊" title="No broker accounts" description="Add a broker account to see daily P&L." />
      ) : (
        <>
          {/* Grand total bar */}
          <div className="panel px-5 py-3 flex items-center justify-between">
            <div className="flex items-center gap-6">
              <div>
                <div className="text-[10px] text-base-500 uppercase tracking-wider">Total P&L</div>
                <PnlValue value={totals.total} prefix="$" decimals={2} className="text-lg font-semibold" />
              </div>
              <div>
                <div className="text-[10px] text-base-500">Realized</div>
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
                  <th className="text-right font-medium px-3 py-2.5 w-24">Realized</th>
                  <th className="text-right font-medium px-3 py-2.5 w-24">Unrealized</th>
                  <th className="text-right font-medium px-3 py-2.5 w-24">Total</th>
                  <th className="text-right font-medium px-3 py-2.5 w-14">Fills</th>
                  <th className="text-right font-medium px-3 py-2.5 w-14">Open</th>
                  <th className="text-center font-medium px-2 py-2.5 w-36">Intraday</th>
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
                        <Sparkline bars={account.bars} />
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

function Sparkline({ bars }) {
  if (!bars.length) {
    return <div className="h-8 flex items-center justify-center text-[10px] text-base-600">—</div>
  }

  const lastVal = bars[bars.length - 1].cumulative_total
  const color = lastVal >= 0 ? '#00e5a0' : '#ff4d4d'

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
                <div className="text-base-400">
                  {new Date(d.period_start).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                </div>
                <div className={d.cumulative_total >= 0 ? 'text-accent' : 'text-loss'}>
                  {d.cumulative_total >= 0 ? '+' : ''}{d.cumulative_total.toFixed(2)}
                </div>
              </div>
            )
          }}
        />
        <Line
          type="monotone"
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
