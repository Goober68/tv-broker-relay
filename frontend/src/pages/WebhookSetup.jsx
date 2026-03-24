import { useAuth } from '../lib/auth-context'
import { apiKeys as apiKeysApi, orders as ordersApi } from '../lib/api'
import { useApi } from '../hooks/useApi'
import { PageSpinner, SectionHeader, StatusBadge, CopyButton, Alert, EmptyState } from '../components/ui'

export default function WebhookSetupPage() {
  const { user } = useAuth()
  const { data: keys, loading: keysLoading } = useApi(() => apiKeysApi.list())
  const { data: deliveries, loading: dlLoading, refetch } = useApi(
    () => ordersApi.deliveries({ limit: 20 })
  )

  const activeKey = keys?.find(k => k.is_active)
  const tenantId  = user?.id
  const webhookUrl = `${window.location.origin}/webhook/${tenantId}`

  const examplePayload = JSON.stringify({
    secret: "ignored",
    broker: "oanda",
    account: "primary",
    action: "{{strategy.order.action}}",
    symbol: "{{ticker}}",
    instrument_type: "forex",
    order_type: "market",
    quantity: 1000,
    comment: "{{strategy.order.comment}}"
  }, null, 2)

  return (
    <div className="space-y-8 animate-fade-in">
      <SectionHeader
        title="Webhook Setup"
        description="Configure TradingView to send alerts to your relay endpoint"
      />

      {/* Step 1 — URL */}
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

      {/* Step 2 — Header */}
      <section className="panel p-6 space-y-4">
        <StepHeader n={2} title="Add the X-Webhook-Secret header" />
        {keysLoading ? (
          <PageSpinner />
        ) : !activeKey ? (
          <Alert
            type="warn"
            message={
              <>No active API key found. <a href="/api-keys" className="underline">Create one first.</a></>
            }
          />
        ) : (
          <>
            <p className="text-xs text-base-400">
              In TradingView alert settings, add a custom header:
            </p>
            <div className="bg-base-950 border border-base-700 rounded-md p-3 space-y-2 font-mono text-sm">
              <div className="flex items-center gap-4">
                <span className="text-base-500 w-28 flex-shrink-0">Header name</span>
                <span className="text-base-100">X-Webhook-Secret</span>
                <CopyButton value="X-Webhook-Secret" />
              </div>
              <div className="flex items-center gap-4">
                <span className="text-base-500 w-28 flex-shrink-0">Value</span>
                <span className="text-base-300 text-xs">{activeKey.key_prefix} (your full key)</span>
              </div>
            </div>
            <p className="text-xs text-base-500">
              Key prefix: <code className="font-mono bg-base-800 px-1 rounded">{activeKey.key_prefix}</code> ·
              The full key was shown once at creation. <a href="/api-keys" className="text-accent hover:underline">Manage keys →</a>
            </p>
          </>
        )}
      </section>

      {/* Step 3 — Payload */}
      <section className="panel p-6 space-y-4">
        <StepHeader n={3} title="Configure the alert message body" />
        <p className="text-xs text-base-400">
          Paste this into the <em>Message</em> field. TradingView will substitute the{' '}
          <code className="font-mono bg-base-800 px-1 rounded text-xs">{'{{variables}}'}</code> automatically.
        </p>
        <div className="relative">
          <pre className="bg-base-950 border border-base-700 rounded-md p-4 text-xs font-mono text-base-300 overflow-x-auto">
            {examplePayload}
          </pre>
          <div className="absolute top-3 right-3">
            <CopyButton value={examplePayload} />
          </div>
        </div>
        <p className="text-xs text-base-500">
          The <code className="font-mono bg-base-800 px-1 rounded">secret</code> field is ignored
          — authentication uses the header instead.
        </p>
      </section>

      {/* Delivery log */}
      <section className="panel overflow-hidden">
        <div className="px-5 py-4 border-b border-base-800 flex items-center justify-between">
          <h2 className="font-display font-semibold text-base-100">Recent deliveries</h2>
          <button onClick={refetch} className="text-xs text-base-400 hover:text-base-200">↻ Refresh</button>
        </div>
        {dlLoading ? (
          <div className="flex justify-center py-10"><PageSpinner /></div>
        ) : !deliveries?.length ? (
          <EmptyState icon="📡" title="No deliveries yet" description="Fire a test alert from TradingView to see it here." />
        ) : (
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Outcome</th>
                  <th>Status</th>
                  <th>Duration</th>
                  <th>IP</th>
                  <th>Error</th>
                </tr>
              </thead>
              <tbody>
                {deliveries.map(d => (
                  <tr key={d.id}>
                    <td><span className="font-mono text-xs text-base-400">{new Date(d.created_at).toLocaleTimeString()}</span></td>
                    <td><StatusBadge status={d.outcome} /></td>
                    <td><span className="font-mono text-xs">{d.http_status}</span></td>
                    <td>
                      <span className="font-mono text-xs text-base-400">
                        {d.duration_ms ? `${d.duration_ms.toFixed(0)}ms` : '—'}
                      </span>
                    </td>
                    <td><span className="font-mono text-xs text-base-500">{d.source_ip || '—'}</span></td>
                    <td>
                      {d.error_detail ? (
                        <span className="text-xs text-loss font-mono truncate max-w-xs block" title={d.error_detail}>
                          {d.error_detail.slice(0, 60)}{d.error_detail.length > 60 ? '…' : ''}
                        </span>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
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
