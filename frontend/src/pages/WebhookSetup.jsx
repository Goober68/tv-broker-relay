import { useState } from 'react'
import { useAuth } from '../lib/auth-context'
import { apiKeys as apiKeysApi, orders as ordersApi } from '../lib/api'
import { useApi } from '../hooks/useApi'
import { PageSpinner, SectionHeader, StatusBadge, CopyButton, Alert, EmptyState } from '../components/ui'

export default function WebhookSetupPage() {
  const { user } = useAuth()
  const [page, setPage] = useState(0)
  const PAGE_SIZE = 25

  const { data: keys, loading: keysLoading } = useApi(() => apiKeysApi.list())
  const { data: deliveries, loading: dlLoading, refetch } = useApi(
    () => ordersApi.deliveries({ limit: PAGE_SIZE, offset: page * PAGE_SIZE }),
    [page]
  )

  const activeKey = keys?.find(k => k.is_active)
  const tenantId  = user?.id
  const webhookUrl = `${window.location.origin}/webhook/${tenantId}`

  // Two payload examples — one for each auth method
  const examplePayloadHeader = JSON.stringify({
    broker: "oanda",
    account: "primary",
    action: "{{strategy.order.action}}",
    symbol: "EUR_USD",
    instrument_type: "forex",
    order_type: "market",
    time_in_force: "FOK",
    quantity: 1000,
    comment: "{{strategy.order.comment}}"
  }, null, 2)

  const examplePayloadSecret = activeKey ? JSON.stringify({
    secret: activeKey.key_prefix + "... (your full key)",
    broker: "oanda",
    account: "primary",
    action: "{{strategy.order.action}}",
    symbol: "EUR_USD",
    instrument_type: "forex",
    order_type: "market",
    time_in_force: "FOK",
    quantity: 1000,
    comment: "{{strategy.order.comment}}"
  }, null, 2) : null

  return (
    <div className="space-y-8 animate-fade-in">
      <SectionHeader
        title="Webhook Setup"
        description="Configure TradingView to send alerts to your relay endpoint"
      />

      {/* Step 1 */}
      <section className="panel p-6 space-y-4">
        <StepHeader n={1} title="Set the Webhook URL in TradingView" />
        <div className="flex items-center gap-3 bg-base-950 border border-base-700 rounded-md px-4 py-3">
          <code className="flex-1 font-mono text-sm text-accent break-all">{webhookUrl}</code>
          <CopyButton value={webhookUrl} />
        </div>
        <p className="text-xs text-base-400">
          In TradingView: <em>Alerts → Create Alert → Notifications → Webhook URL</em>
        </p>
      </section>

      {/* Step 2 */}
      <section className="panel p-6 space-y-4">
        <StepHeader n={2} title="Add the X-Webhook-Secret header" />
        {keysLoading ? (
          <PageSpinner />
        ) : !activeKey ? (
          <Alert type="warn" message={<>No active API key. <a href="/api-keys" className="underline">Create one first.</a></>} />
        ) : (
          <>
            <p className="text-xs text-base-400">In TradingView alert settings, add a custom header:</p>
            <div className="bg-base-950 border border-base-700 rounded-md p-3 space-y-2 font-mono text-sm">
              <div className="flex items-center gap-4">
                <span className="text-base-500 w-28 flex-shrink-0">Header name</span>
                <span className="text-base-100">X-Webhook-Secret</span>
                <CopyButton value="X-Webhook-Secret" />
              </div>
              <div className="flex items-center gap-4">
                <span className="text-base-500 w-28 flex-shrink-0">Value</span>
                <span className="text-base-300 text-xs">{activeKey.key_prefix}… (your full key)</span>
              </div>
            </div>
            <p className="text-xs text-base-500">
              Key prefix: <code className="font-mono bg-base-800 px-1 rounded">{activeKey.key_prefix}</code> ·{' '}
              <a href="/api-keys" className="text-accent hover:underline">Manage keys →</a>
            </p>
          </>
        )}
      </section>

      {/* Step 3 */}
      <section className="panel p-6 space-y-4">
        <StepHeader n={3} title="Configure the alert message body" />
        <p className="text-xs text-base-400">
          Paste this into the <em>Message</em> field. TradingView will substitute the{' '}
          <code className="font-mono bg-base-800 px-1 rounded text-xs">{'{{variables}}'}</code> automatically.
        </p>

        {/* Auth method tabs */}
        <AuthMethodPayload
          payloadHeader={examplePayloadHeader}
          payloadSecret={examplePayloadSecret}
        />
      </section>

      {/* Delivery log */}
      <section className="panel overflow-hidden">
        <div className="px-5 py-4 border-b border-base-800 flex items-center justify-between">
          <h2 className="font-display font-semibold text-base-100">Recent deliveries</h2>
          <div className="flex items-center gap-3">
            <span className="text-xs text-base-500">Click a row to inspect</span>
            {/* Paging controls */}
            <div className="flex items-center gap-1">
              <button
                onClick={() => setPage(p => Math.max(0, p - 1))}
                disabled={page === 0}
                className="text-xs text-base-400 hover:text-base-200 disabled:opacity-30 disabled:cursor-not-allowed px-1.5 py-0.5 rounded hover:bg-base-700"
              >
                ‹
              </button>
              <span className="text-xs text-base-500 font-mono px-1">
                {page + 1}
              </span>
              <button
                onClick={() => setPage(p => p + 1)}
                disabled={!deliveries || deliveries.length < PAGE_SIZE}
                className="text-xs text-base-400 hover:text-base-200 disabled:opacity-30 disabled:cursor-not-allowed px-1.5 py-0.5 rounded hover:bg-base-700"
              >
                ›
              </button>
            </div>
            <button onClick={() => { setPage(0); refetch() }} className="text-xs text-base-400 hover:text-base-200">↻</button>
          </div>
        </div>
        {dlLoading ? (
          <div className="flex justify-center py-10"><PageSpinner /></div>
        ) : !deliveries?.length ? (
          <EmptyState icon="📡" title="No deliveries yet" description="Fire a test alert from TradingView to see it here." />
        ) : (
          <div className="divide-y divide-base-800">
            {deliveries.map(d => <DeliveryRow key={d.id} delivery={d} />)}
          </div>
        )}
      </section>
    </div>
  )
}

function DeliveryRow({ delivery: d }) {
  const [expanded, setExpanded] = useState(false)

  // Parse raw_payload for pretty display
  let parsedPayload = null
  let parseError = null
  if (d.raw_payload) {
    try {
      parsedPayload = JSON.parse(d.raw_payload)
    } catch {
      parseError = d.raw_payload
    }
  }

  return (
    <div>
      {/* Summary row — clickable */}
      <div
        className="px-5 py-3 flex items-center gap-4 cursor-pointer hover:bg-base-800/40 transition-colors select-none"
        onClick={() => setExpanded(v => !v)}
      >
        {/* Expand toggle */}
        <span className="text-base-600 text-xs font-mono w-3 flex-shrink-0">
          {expanded ? '▼' : '▶'}
        </span>

        {/* Time */}
        <span className="font-mono text-xs text-base-400 w-20 flex-shrink-0">
          {new Date(d.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
        </span>

        {/* Outcome */}
        <StatusBadge status={d.outcome} />

        {/* HTTP status */}
        <span className="font-mono text-xs text-base-400 w-8">{d.http_status}</span>

        {/* Duration */}
        <span className="font-mono text-xs text-base-500 w-16">
          {d.duration_ms ? `${d.duration_ms.toFixed(0)}ms` : '—'}
        </span>

        {/* IP */}
        <span className="font-mono text-xs text-base-500 flex-1">{d.source_ip || '—'}</span>

        {/* Error summary */}
        {d.error_detail && (
          <span className="text-xs text-loss font-mono truncate max-w-xs">
            {d.error_detail.slice(0, 50)}{d.error_detail.length > 50 ? '…' : ''}
          </span>
        )}

        {/* Auth badge */}
        <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded flex-shrink-0 ${
          d.auth_passed ? 'bg-accent/10 text-accent' : 'bg-loss/10 text-loss'
        }`}>
          {d.auth_passed ? 'auth ok' : 'auth fail'}
        </span>
      </div>

      {/* Expanded detail panel */}
      {expanded && (
        <div className="border-t border-base-800 bg-base-900/60 px-5 py-4 space-y-4 animate-fade-in">

          {/* Headers section */}
          <div>
            <div className="text-[10px] font-mono text-base-500 uppercase tracking-wider mb-2">
              Received headers
            </div>
            <div className="bg-base-950 border border-base-800 rounded-md p-3 space-y-1.5">
              <HeaderRow label="X-Webhook-Secret" value={d.auth_passed ? '••••••••••••••••' : '(missing or invalid)'} />
              <HeaderRow label="User-Agent" value={d.user_agent || '—'} />
              <HeaderRow label="Source IP" value={d.source_ip || '—'} />
              <HeaderRow label="Auth result" value={d.auth_passed ? '✓ passed' : '✗ failed'} highlight={d.auth_passed ? 'green' : 'red'} />
            </div>
          </div>

          {/* Payload section */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <div className="text-[10px] font-mono text-base-500 uppercase tracking-wider">
                Raw payload
              </div>
              {d.raw_payload && (
                <CopyButton value={d.raw_payload} label="Copy" />
              )}
            </div>
            {!d.raw_payload ? (
              <p className="text-xs text-base-600 italic">No payload captured</p>
            ) : parseError ? (
              <pre className="bg-base-950 border border-base-800 rounded-md p-3 text-xs font-mono text-base-300 overflow-x-auto whitespace-pre-wrap break-all">
                {parseError}
              </pre>
            ) : (
              <pre className="bg-base-950 border border-base-800 rounded-md p-3 text-xs font-mono text-base-300 overflow-x-auto">
                {JSON.stringify(parsedPayload, null, 2)}
              </pre>
            )}
          </div>

          {/* Error detail if present */}
          {d.error_detail && (
            <div>
              <div className="text-[10px] font-mono text-base-500 uppercase tracking-wider mb-2">
                Error detail
              </div>
              <pre className="bg-base-950 border border-loss/20 rounded-md p-3 text-xs font-mono text-loss overflow-x-auto whitespace-pre-wrap">
                {d.error_detail}
              </pre>
            </div>
          )}

          {/* Order link if created */}
          {d.order_id && (
            <div className="text-xs text-base-400">
              Order created: <a href="/orders" className="text-accent hover:underline font-mono">#{d.order_id}</a>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function HeaderRow({ label, value, highlight }) {
  const valueColor = highlight === 'green' ? 'text-accent'
    : highlight === 'red' ? 'text-loss'
    : 'text-base-300'
  return (
    <div className="flex items-start gap-3 text-xs">
      <span className="font-mono text-base-500 w-36 flex-shrink-0">{label}</span>
      <span className={`font-mono break-all ${valueColor}`}>{value}</span>
    </div>
  )
}

function AuthMethodPayload({ payloadHeader, payloadSecret }) {
  const [method, setMethod] = useState('header')
  const payload = method === 'header' ? payloadHeader : payloadSecret

  return (
    <div className="space-y-3">
      {/* Method selector */}
      <div className="flex gap-2">
        <button
          onClick={() => setMethod('header')}
          className={`px-3 py-1.5 text-xs font-mono rounded transition-colors ${
            method === 'header'
              ? 'bg-accent/20 text-accent border border-accent/30'
              : 'bg-base-800 text-base-400 border border-base-700 hover:text-base-200'
          }`}
        >
          Paid plan — header auth
        </button>
        <button
          onClick={() => setMethod('secret')}
          className={`px-3 py-1.5 text-xs font-mono rounded transition-colors ${
            method === 'secret'
              ? 'bg-accent/20 text-accent border border-accent/30'
              : 'bg-base-800 text-base-400 border border-base-700 hover:text-base-200'
          }`}
        >
          Free plan — secret in payload
        </button>
      </div>

      {/* Description */}
      <p className="text-xs text-base-500">
        {method === 'header' ? (
          <>
            Secret is sent as the <code className="font-mono bg-base-800 px-1 rounded">X-Webhook-Secret</code> header.
            Available on TradingView Pro, Pro+, and Premium plans.
          </>
        ) : (
          <>
            Secret is included directly in the JSON payload as the <code className="font-mono bg-base-800 px-1 rounded">secret</code> field.
            Works on all TradingView plans including free.
          </>
        )}
      </p>

      {/* Payload */}
      <div className="relative">
        <pre className="bg-base-950 border border-base-700 rounded-md p-4 text-xs font-mono text-base-300 overflow-x-auto">
          {payload}
        </pre>
        <div className="absolute top-3 right-3">
          <CopyButton value={payload || ''} />
        </div>
      </div>

      {method === 'secret' && (
        <p className="text-xs text-warn">
          Keep your secret secure — anyone with this URL and secret can execute trades on your account.
        </p>
      )}
    </div>
  )
}

function StepHeader({ n, title }) {
  return (
    <div className="flex items-center gap-3">
      <div className="w-6 h-6 rounded-full bg-accent/20 border border-accent/30 flex items-center justify-center flex-shrink-0">
        <span className="text-accent text-xs font-bold font-mono">{n}</span>
      </div>
      <h3 className="font-display font-semibold text-base-100">{title}</h3>
    </div>
  )
}
