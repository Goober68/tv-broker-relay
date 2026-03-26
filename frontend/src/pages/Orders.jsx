import { useState } from 'react'
import { orders as ordersApi } from '../lib/api'
import { useApi } from '../hooks/useApi'
import {
  PageSpinner, SectionHeader, StatusBadge, Mono, EmptyState
} from '../components/ui'

const BROKERS  = ['', 'oanda', 'ibkr', 'tradovate', 'etrade']
const STATUSES = ['', 'filled', 'open', 'submitted', 'cancelled', 'rejected', 'error']

export default function OrdersPage() {
  const [broker, setBroker]   = useState('')
  const [status, setStatus]   = useState('')
  const [symbol, setSymbol]   = useState('')

  const params = {}
  if (broker) params.broker = broker
  if (status) params.status = status
  if (symbol) params.symbol = symbol.toUpperCase()
  params.limit = 100

  const { data, loading, refetch } = useApi(
    () => ordersApi.list(params),
    [broker, status, symbol]
  )

  return (
    <div className="space-y-6 animate-fade-in">
      <SectionHeader title="Orders" description="Complete execution history" />

      {/* Filters */}
      <div className="flex flex-wrap gap-3 panel p-4">
        <input
          className="input w-36 py-1.5"
          placeholder="Symbol…"
          value={symbol}
          onChange={e => setSymbol(e.target.value)}
        />
        <select
          className="input w-36 py-1.5"
          value={broker}
          onChange={e => setBroker(e.target.value)}
        >
          <option value="">All brokers</option>
          {BROKERS.filter(Boolean).map(b => (
            <option key={b} value={b}>{b}</option>
          ))}
        </select>
        <select
          className="input w-40 py-1.5"
          value={status}
          onChange={e => setStatus(e.target.value)}
        >
          <option value="">All statuses</option>
          {STATUSES.filter(Boolean).map(s => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
        <button onClick={refetch} className="btn-ghost py-1.5 px-3 text-xs">↻ Refresh</button>
      </div>

      {/* Table */}
      <div className="panel overflow-hidden">
        {loading ? (
          <div className="flex justify-center py-16"><PageSpinner /></div>
        ) : !data?.length ? (
          <EmptyState icon="📋" title="No orders found" description="Try adjusting the filters." />
        ) : (
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Symbol</th>
                  <th>Action</th>
                  <th>Type</th>
                  <th>Qty</th>
                  <th>Broker Qty</th>
                  <th>Price</th>
                  <th>Fill price</th>
                  <th>Broker</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {data.map(order => <OrderRow key={order.id} order={order} />)}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {data && (
        <p className="text-xs text-base-500 text-right">
          Showing {data.length} order{data.length !== 1 ? 's' : ''}
        </p>
      )}
    </div>
  )
}

function OrderRow({ order }) {
  const isBuy  = order.action === 'buy'
  const isClose = order.action === 'close'
  const actionColor = isBuy
    ? 'text-accent'
    : isClose ? 'text-warn' : 'text-loss'

  return (
    <tr>
      <td>
        <span className="font-mono text-xs text-base-400">
          {new Date(order.created_at).toLocaleString([], {
            month: 'short', day: 'numeric',
            hour: '2-digit', minute: '2-digit'
          })}
        </span>
      </td>
      <td>
        <span className="font-mono font-medium text-base-100">{order.symbol}</span>
        {order.instrument_type !== 'forex' && (
          <span className="ml-1 text-[10px] text-base-500">{order.instrument_type}</span>
        )}
      </td>
      <td>
        <span className={`font-mono font-semibold uppercase text-xs ${actionColor}`}>
          {order.action}
        </span>
      </td>
      <td><span className="text-base-400 text-xs font-mono">{order.order_type}</span></td>
      <td><Mono>{order.quantity.toLocaleString()}</Mono></td>
      <td>
        {order.broker_quantity != null && order.broker_quantity !== order.quantity ? (
          <div className="text-right">
            <Mono className="text-warn">{order.broker_quantity.toLocaleString()}</Mono>
            <div className="text-[10px] text-base-600 font-mono">randomized</div>
          </div>
        ) : (
          <Mono className="text-base-500">—</Mono>
        )}
      </td>
      <td>
        <Mono className="text-base-400">
          {order.price ? order.price.toFixed(5) : '—'}
        </Mono>
      </td>
      <td>
        <Mono>
          {order.avg_fill_price ? order.avg_fill_price.toFixed(5) : '—'}
        </Mono>
      </td>
      <td>
        <span className="text-xs text-base-400">{order.broker}</span>
      </td>
      <td><StatusBadge status={order.status} /></td>
    </tr>
  )
}
