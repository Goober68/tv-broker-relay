import { useState } from 'react'
import { apiKeys as apiKeysApi } from '../lib/api'
import { useApi } from '../hooks/useApi'
import {
  PageSpinner, SectionHeader, Alert, EmptyState,
  CopyButton, ConfirmInline, Spinner
} from '../components/ui'

export default function ApiKeysPage() {
  const { data, loading, error, refetch } = useApi(() => apiKeysApi.list())
  const [creating, setCreating]   = useState(false)
  const [newName, setNewName]     = useState('')
  const [newKey, setNewKey]       = useState(null)   // raw key shown once
  const [createErr, setCreateErr] = useState(null)
  const [createLoading, setCreateLoading] = useState(false)
  const [confirmRevoke, setConfirmRevoke] = useState(null)  // key id

  const handleCreate = async (e) => {
    e.preventDefault()
    if (!newName.trim()) return
    setCreateLoading(true)
    setCreateErr(null)
    try {
      const created = await apiKeysApi.create(newName.trim())
      setNewKey(created.raw_key)
      setNewName('')
      setCreating(false)
      refetch()
    } catch (err) {
      setCreateErr(err.detail || 'Failed to create key')
    } finally {
      setCreateLoading(false)
    }
  }

  const handleRevoke = async (id) => {
    try {
      await apiKeysApi.revoke(id)
      setConfirmRevoke(null)
      refetch()
    } catch {}
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <SectionHeader
        title="API Keys"
        description="Used to authenticate TradingView webhooks"
        action={
          !creating && (
            <button onClick={() => setCreating(true)} className="btn-primary">
              + New key
            </button>
          )
        }
      />

      {/* New key just created — show raw key ONCE */}
      {newKey && (
        <div className="panel p-5 border-accent/30 bg-accent/5 animate-slide-up">
          <div className="flex items-start justify-between mb-2">
            <div>
              <p className="text-sm font-semibold text-accent">Key created — copy it now</p>
              <p className="text-xs text-base-400 mt-0.5">This is the only time the full key will be shown.</p>
            </div>
            <button onClick={() => setNewKey(null)} className="text-base-500 hover:text-base-300 text-lg leading-none">×</button>
          </div>
          <div className="flex items-center gap-3 bg-base-950 border border-base-700 rounded-md px-4 py-3 mt-3">
            <code className="flex-1 font-mono text-sm text-base-100 break-all">{newKey}</code>
            <CopyButton value={newKey} label="Copy key" />
          </div>
          <p className="text-xs text-base-500 mt-3">
            Add this as an <code className="font-mono bg-base-800 px-1 py-0.5 rounded">X-Webhook-Secret</code> header in your TradingView alert.
          </p>
        </div>
      )}

      {/* Create form */}
      {creating && (
        <div className="panel p-5 animate-slide-up">
          <form onSubmit={handleCreate} className="flex items-end gap-3">
            <div className="flex-1">
              <label className="block text-xs font-medium text-base-300 mb-1.5">Key name</label>
              <input
                className="input"
                placeholder='e.g. "TradingView Production"'
                value={newName}
                onChange={e => setNewName(e.target.value)}
                autoFocus
              />
            </div>
            <button type="submit" className="btn-primary flex items-center gap-2" disabled={createLoading}>
              {createLoading && <Spinner size="sm" />}
              Create
            </button>
            <button type="button" className="btn-ghost" onClick={() => { setCreating(false); setCreateErr(null) }}>
              Cancel
            </button>
          </form>
          <Alert type="error" message={createErr} className="mt-3" />
        </div>
      )}

      {/* Key list */}
      <div className="panel overflow-hidden">
        {loading ? (
          <div className="flex justify-center py-16"><PageSpinner /></div>
        ) : error ? (
          <Alert type="error" message={error} className="m-5" />
        ) : !data?.length ? (
          <EmptyState
            icon="🔑"
            title="No API keys yet"
            description="Create a key to start receiving TradingView webhooks."
            action={<button onClick={() => setCreating(true)} className="btn-primary">Create first key</button>}
          />
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Prefix</th>
                <th>Created</th>
                <th>Last used</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {data.map(key => (
                <tr key={key.id} className={!key.is_active ? 'opacity-40' : ''}>
                  <td className="font-medium text-base-100">{key.name}</td>
                  <td><code className="font-mono text-xs text-base-400">{key.key_prefix}</code></td>
                  <td><span className="text-xs text-base-400 font-mono">{fmtDate(key.created_at)}</span></td>
                  <td><span className="text-xs text-base-400 font-mono">{key.last_used_at ? fmtDate(key.last_used_at) : '—'}</span></td>
                  <td>
                    <span className={`badge ${key.is_active ? 'badge-green' : 'badge-neutral'}`}>
                      {key.is_active ? 'active' : 'revoked'}
                    </span>
                  </td>
                  <td>
                    {key.is_active && (
                      confirmRevoke === key.id ? (
                        <ConfirmInline
                          message="Revoke this key? Any alerts using it will fail."
                          onConfirm={() => handleRevoke(key.id)}
                          onCancel={() => setConfirmRevoke(null)}
                          dangerous
                        />
                      ) : (
                        <button
                          onClick={() => setConfirmRevoke(key.id)}
                          className="text-xs text-base-500 hover:text-loss transition-colors"
                        >
                          Revoke
                        </button>
                      )
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}

function fmtDate(d) {
  return new Date(d).toLocaleDateString([], { month: 'short', day: 'numeric', year: '2-digit' })
}
