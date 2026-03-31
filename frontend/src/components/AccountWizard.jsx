import { useState, useEffect } from 'react'
import { brokerAccounts as brokersApi } from '../lib/api'
import { Alert, Spinner, brokerLabel, BrokerIcon } from '../components/ui'
import { clsx } from 'clsx'

const ACCOUNT_TYPES = [
  { key: 'personal-demo', label: 'Personal Demo', group: 'personal' },
  { key: 'personal-live', label: 'Personal Live', group: 'personal' },
  { key: 'prop-eval',     label: 'Prop Eval',     group: 'prop' },
  { key: 'prop-demo',     label: 'Prop Demo',     group: 'prop' },
  { key: 'prop-live',     label: 'Prop Live',     group: 'prop' },
]

const OANDA_URLS = {
  'personal-demo': 'https://api-fxpractice.oanda.com/v3',
  'personal-live': 'https://api-fxtrade.oanda.com/v3',
  'prop-eval':     'https://api-fxpractice.oanda.com/v3',
  'prop-demo':     'https://api-fxpractice.oanda.com/v3',
  'prop-live':     'https://api-fxtrade.oanda.com/v3',
}

const TV_ENVS = {
  'personal-demo': 'demo',
  'personal-live': 'live',
  'prop-eval':     'demo',
  'prop-demo':     'demo',
  'prop-live':     'live',
}

export default function AccountWizard({ onDone, onCancel, oauthData }) {
  const [step, setStep] = useState(oauthData ? 3 : 1)
  const [broker, setBroker] = useState(oauthData ? 'tradovate' : null)
  const [accountType, setAccountType] = useState(oauthData?.env === 'demo' ? 'prop-demo' : 'prop-live')
  const [error, setError] = useState(null)

  // Step 3 state
  const [oandaAccountId, setOandaAccountId] = useState('')
  const [oandaAlias, setOandaAlias] = useState('')
  const [oandaApiKey, setOandaApiKey] = useState('')
  const [verifying, setVerifying] = useState(false)
  const [verified, setVerified] = useState(null) // { balance, currency }
  const [tvAccounts, setTvAccounts] = useState(oauthData?.accounts || null)
  const [tvSelected, setTvSelected] = useState({})
  const [tvOAuthToken, setTvOAuthToken] = useState(oauthData?.token || null)

  // Step 4 state
  const [autoClose, setAutoClose] = useState(false)
  const [autoCloseTime, setAutoCloseTime] = useState('16:50')
  const [fifo, setFifo] = useState(true)
  const [maxTotalDD, setMaxTotalDD] = useState('')
  const [maxDailyDD, setMaxDailyDD] = useState('')

  // Step 5 state
  const [syncing, setSyncing] = useState(false)
  const [syncResult, setSyncResult] = useState(null)
  const [saving, setSaving] = useState(false)

  const isProp = accountType?.startsWith('prop')

  // Pre-populate Tradovate account selection from OAuth data
  useEffect(() => {
    if (oauthData?.accounts && Object.keys(tvSelected).length === 0) {
      const sel = {}
      for (const a of oauthData.accounts) {
        const shortName = a.name.length > 6 ? a.name.slice(-3) : a.name
        sel[a.name] = { checked: true, alias: shortName }
      }
      setTvSelected(sel)
    }
  }, [oauthData])

  // Set defaults when account type changes
  useEffect(() => {
    setAutoClose(isProp)
  }, [accountType])

  // ── Step handlers ──

  const handleVerifyOanda = async () => {
    setVerifying(true)
    setError(null)
    setVerified(null)
    try {
      const result = await brokersApi.verifyConnection({
        broker: 'oanda',
        account_alias: oandaAccountId,
        credentials: {
          api_key: oandaApiKey,
          account_id: oandaAccountId,
          base_url: OANDA_URLS[accountType],
        },
      })
      setVerified(result)
    } catch (err) {
      setError(err.detail || 'Connection failed')
    } finally {
      setVerifying(false)
    }
  }

  const handleTradovateOAuth = async () => {
    try {
      const env = TV_ENVS[accountType]
      const { url } = await brokersApi.tradovateOAuthUrl(env)
      window.location.href = url
    } catch (err) {
      setError(err.detail || 'Failed to start OAuth')
    }
  }

  const handleSaveOanda = async () => {
    setSaving(true)
    setError(null)
    try {
      await brokersApi.create({
        broker: 'oanda',
        account_alias: oandaAccountId,
        display_name: oandaAlias || null,
        account_type: accountType,
        auto_close_enabled: autoClose,
        auto_close_time: autoClose ? autoCloseTime : null,
        fifo_randomize: fifo,
        credentials: {
          api_key: oandaApiKey,
          account_id: oandaAccountId,
          base_url: OANDA_URLS[accountType],
        },
      })
      // Save drawdown limits if set
      // (handled after account creation via the drawdown endpoint)
      setStep(5)
    } catch (err) {
      setError(err.detail || 'Failed to save')
    } finally {
      setSaving(false)
    }
  }

  const handleSaveTradovate = async () => {
    setSaving(true)
    setError(null)
    try {
      const accounts = Object.entries(tvSelected)
        .filter(([, v]) => v.checked)
        .map(([name, v]) => ({
          name,
          alias: v.alias || name,
          display_name: v.alias || name,
          prop_firm: isProp,
        }))
      if (!accounts.length) {
        setError('Select at least one account')
        setSaving(false)
        return
      }
      const credsPayload = tvOAuthToken
        ? { _encrypted: tvOAuthToken }
        : {}
      await brokersApi.tradovateBulkCreate(credsPayload, accounts)
      setStep(5)
    } catch (err) {
      setError(err.detail || 'Failed to create accounts')
    } finally {
      setSaving(false)
    }
  }

  // ── Step indicator ──

  const steps = ['Broker', 'Type', 'Connect', 'Configure', 'Sync']

  return (
    <div className="panel p-6 animate-slide-up">
      {/* Step indicator */}
      <div className="flex items-center gap-2 mb-6">
        {steps.map((label, i) => (
          <div key={i} className="flex items-center gap-2">
            <div className={clsx(
              'w-6 h-6 rounded-full flex items-center justify-center text-[10px] font-mono font-bold',
              step > i + 1 ? 'bg-accent/20 text-accent' :
              step === i + 1 ? 'bg-accent text-base-950' :
              'bg-base-700 text-base-500'
            )}>
              {step > i + 1 ? '✓' : i + 1}
            </div>
            <span className={clsx(
              'text-xs',
              step === i + 1 ? 'text-base-100 font-medium' : 'text-base-500'
            )}>{label}</span>
            {i < steps.length - 1 && <div className="w-6 h-px bg-base-700" />}
          </div>
        ))}
      </div>

      <h3 className="font-display font-semibold text-base-100 mb-4">Connect a broker account</h3>

      {/* ── Step 1: Select broker ── */}
      {step === 1 && (
        <div className="space-y-4">
          <p className="text-xs text-base-400">Select your brokerage</p>
          <div className="flex gap-3">
            {['oanda', 'tradovate'].map(b => (
              <button
                key={b}
                onClick={() => { setBroker(b); setStep(2) }}
                className="flex-1 py-4 px-4 rounded-lg border border-base-700 bg-base-900 hover:border-accent/40 hover:bg-base-800 transition-colors text-center"
              >
                <BrokerIcon broker={b} size={28} className="mx-auto mb-2" />
                <div className="text-sm font-medium text-base-100">{brokerLabel(b)}</div>
                <div className="text-[10px] text-base-500 mt-1">
                  {b === 'oanda' ? 'Forex & CFD' : 'Futures'}
                </div>
              </button>
            ))}
          </div>
          <div className="flex gap-3 pt-2">
            <button type="button" className="btn-ghost" onClick={onCancel}>Cancel</button>
          </div>
        </div>
      )}

      {/* ── Step 2: Account type ── */}
      {step === 2 && (
        <div className="space-y-4">
          <p className="text-xs text-base-400">What type of {brokerLabel(broker)} account?</p>
          <div className="space-y-2">
            <p className="text-[10px] text-base-500 uppercase tracking-wider">Personal</p>
            <div className="flex gap-2">
              {ACCOUNT_TYPES.filter(t => t.group === 'personal').map(t => (
                <button
                  key={t.key}
                  onClick={() => { setAccountType(t.key); setStep(3) }}
                  className="flex-1 py-3 px-3 rounded-md border border-base-700 bg-base-900 hover:border-accent/40 hover:bg-base-800 transition-colors text-xs font-medium text-base-200"
                >
                  {t.label}
                </button>
              ))}
            </div>
            <p className="text-[10px] text-base-500 uppercase tracking-wider mt-3">Prop Firm</p>
            <div className="flex gap-2">
              {ACCOUNT_TYPES.filter(t => t.group === 'prop').map(t => (
                <button
                  key={t.key}
                  onClick={() => { setAccountType(t.key); setStep(3) }}
                  className="flex-1 py-3 px-3 rounded-md border border-base-700 bg-base-900 hover:border-accent/40 hover:bg-base-800 transition-colors text-xs font-medium text-base-200"
                >
                  {t.label}
                </button>
              ))}
            </div>
          </div>
          <div className="flex gap-3 pt-2">
            <button type="button" className="btn-ghost" onClick={() => setStep(1)}>Back</button>
            <button type="button" className="btn-ghost" onClick={onCancel}>Cancel</button>
          </div>
        </div>
      )}

      {/* ── Step 3: Connect ── */}
      {step === 3 && broker === 'oanda' && (
        <div className="space-y-4">
          <p className="text-xs text-base-400">
            Enter your {OANDA_URLS[accountType]?.includes('practice') ? 'practice' : 'live'} Oanda credentials
          </p>
          <div className="space-y-3">
            <div>
              <label className="block text-xs font-medium text-base-300 mb-1">Account number</label>
              <input className="input" placeholder="101-001-XXXXXXX-001" value={oandaAccountId}
                onChange={e => { setOandaAccountId(e.target.value); setVerified(null) }} />
            </div>
            <div>
              <label className="block text-xs font-medium text-base-300 mb-1">Account alias</label>
              <input className="input" placeholder="My Oanda Demo" value={oandaAlias}
                onChange={e => setOandaAlias(e.target.value)} />
              <p className="text-[10px] text-base-500 mt-1">
                Friendly name used in webhooks and throughout the UI
              </p>
            </div>
            <div>
              <label className="block text-xs font-medium text-base-300 mb-1">API Key</label>
              <input className="input" type="password" placeholder="Your Oanda API key" value={oandaApiKey}
                onChange={e => { setOandaApiKey(e.target.value); setVerified(null) }} />
            </div>
          </div>

          {verified && (
            <div className="bg-accent/10 border border-accent/30 rounded-md p-3 text-xs text-accent">
              Connected — Balance: {verified.currency} {Number(verified.balance).toLocaleString(undefined, {minimumFractionDigits: 2})}
            </div>
          )}

          <Alert type="error" message={error} />

          <div className="flex gap-3 pt-2">
            {!verified ? (
              <button onClick={handleVerifyOanda} disabled={verifying || !oandaAccountId || !oandaApiKey}
                className="btn-primary flex items-center gap-2">
                {verifying && <Spinner size="sm" />}
                {verifying ? 'Verifying...' : 'Verify connection'}
              </button>
            ) : (
              <button onClick={() => setStep(4)} className="btn-primary">Next: Configure</button>
            )}
            <button type="button" className="btn-ghost" onClick={() => setStep(2)}>Back</button>
            <button type="button" className="btn-ghost" onClick={onCancel}>Cancel</button>
          </div>
        </div>
      )}

      {step === 3 && broker === 'tradovate' && !tvAccounts && (
        <div className="space-y-4">
          <p className="text-xs text-base-400">
            Connect to Tradovate ({TV_ENVS[accountType]}) via OAuth
          </p>
          <button
            onClick={handleTradovateOAuth}
            className="w-full flex items-center justify-center gap-2 px-4 py-3 rounded-md bg-orange-500/10 border border-orange-500/30 text-orange-400 hover:bg-orange-500/20 transition-colors text-sm font-medium"
          >
            Connect with Tradovate
          </button>
          <p className="text-[10px] text-base-500 text-center">
            You'll be redirected to Tradovate to authorize access
          </p>
          <Alert type="error" message={error} />
          <div className="flex gap-3 pt-2">
            <button type="button" className="btn-ghost" onClick={() => setStep(2)}>Back</button>
            <button type="button" className="btn-ghost" onClick={onCancel}>Cancel</button>
          </div>
        </div>
      )}

      {step === 3 && broker === 'tradovate' && tvAccounts && (
        <div className="space-y-4">
          <p className="text-xs text-base-400">
            {tvAccounts.length} account{tvAccounts.length !== 1 ? 's' : ''} found — set aliases and select which to add
          </p>
          <div className="flex items-center justify-end gap-2 mb-1">
            <button type="button" onClick={() => {
              const sel = { ...tvSelected }; for (const k of Object.keys(sel)) sel[k] = { ...sel[k], checked: true }; setTvSelected(sel)
            }} className="text-[10px] text-base-400 hover:text-base-200">Select all</button>
            <button type="button" onClick={() => {
              const sel = { ...tvSelected }; for (const k of Object.keys(sel)) sel[k] = { ...sel[k], checked: false }; setTvSelected(sel)
            }} className="text-[10px] text-base-400 hover:text-base-200">Select none</button>
          </div>
          <div className="max-h-64 overflow-y-auto space-y-1 bg-base-950 rounded-md p-2">
            {tvAccounts.map(a => {
              const s = tvSelected[a.name] || { checked: false, alias: '' }
              return (
                <div key={a.name} className={`flex items-center gap-3 px-3 py-2 rounded transition-colors ${s.checked ? 'bg-base-800/60' : 'opacity-50'}`}>
                  <input type="checkbox" checked={s.checked} className="accent-accent flex-shrink-0"
                    onChange={e => setTvSelected(prev => ({ ...prev, [a.name]: { ...prev[a.name], checked: e.target.checked } }))} />
                  <div className="font-mono text-xs text-base-300 truncate flex-1" title={a.name}>{a.name}</div>
                  <input className="input py-1 text-xs font-mono w-28" placeholder="Alias"
                    value={s.alias}
                    onChange={e => setTvSelected(prev => ({ ...prev, [a.name]: { ...prev[a.name], alias: e.target.value } }))} />
                </div>
              )
            })}
          </div>
          <Alert type="error" message={error} />
          <div className="flex gap-3 pt-2">
            <button onClick={() => setStep(4)} className="btn-primary"
              disabled={!Object.values(tvSelected).some(v => v.checked)}>
              Next: Configure
            </button>
            <button type="button" className="btn-ghost" onClick={onCancel}>Cancel</button>
          </div>
        </div>
      )}

      {/* ── Step 4: Configure ── */}
      {step === 4 && (
        <div className="space-y-4">
          <p className="text-xs text-base-400">Configure account settings</p>

          <div className="grid grid-cols-2 gap-4">
            {/* Auto-close */}
            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="text-xs font-medium text-base-300">Auto-close positions</label>
                <div onClick={() => setAutoClose(v => !v)}
                  className={`w-8 h-4 rounded-full transition-colors relative cursor-pointer ${autoClose ? 'bg-accent' : 'bg-base-600'}`}>
                  <div className={`absolute top-0.5 w-3 h-3 rounded-full bg-white transition-all ${autoClose ? 'left-4' : 'left-0.5'}`} />
                </div>
              </div>
              {autoClose && (
                <input type="time" className="input py-1 text-xs font-mono w-full" value={autoCloseTime}
                  onChange={e => setAutoCloseTime(e.target.value)} />
              )}
              <p className="text-[10px] text-base-500 mt-1">
                {isProp ? 'Recommended for prop accounts' : 'Optional for personal accounts'}
              </p>
            </div>

            {/* FIFO — Oanda only */}
            {broker === 'oanda' && (
              <div>
                <div className="flex items-center justify-between mb-1">
                  <label className="text-xs font-medium text-base-300">FIFO avoidance</label>
                  <div onClick={() => setFifo(v => !v)}
                    className={`w-8 h-4 rounded-full transition-colors relative cursor-pointer ${fifo ? 'bg-accent' : 'bg-base-600'}`}>
                    <div className={`absolute top-0.5 w-3 h-3 rounded-full bg-white transition-all ${fifo ? 'left-4' : 'left-0.5'}`} />
                  </div>
                </div>
                <p className="text-[10px] text-base-500 mt-1">
                  Required for US NFA FIFO compliance
                </p>
              </div>
            )}
          </div>

          {/* Drawdown limits */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-[10px] text-base-500 mb-1">Max total drawdown ($)</label>
              <input className="input py-1 text-xs font-mono" type="number" step="0.01" min="0"
                placeholder="e.g. 2500" value={maxTotalDD} onChange={e => setMaxTotalDD(e.target.value)} />
            </div>
            <div>
              <label className="block text-[10px] text-base-500 mb-1">Max daily drawdown ($)</label>
              <input className="input py-1 text-xs font-mono" type="number" step="0.01" min="0"
                placeholder="e.g. 1500" value={maxDailyDD} onChange={e => setMaxDailyDD(e.target.value)} />
            </div>
          </div>
          <p className="text-[10px] text-base-500">
            Leave blank if no drawdown limits. Commissions can be set per product in the instrument map after setup.
          </p>

          <Alert type="error" message={error} />

          <div className="flex gap-3 pt-2">
            <button onClick={broker === 'oanda' ? handleSaveOanda : handleSaveTradovate}
              disabled={saving} className="btn-primary flex items-center gap-2">
              {saving && <Spinner size="sm" />}
              {saving ? 'Saving...' : 'Save & Continue'}
            </button>
            <button type="button" className="btn-ghost" onClick={() => setStep(3)}>Back</button>
            <button type="button" className="btn-ghost" onClick={onCancel}>Cancel</button>
          </div>
        </div>
      )}

      {/* ── Step 5: Sync ── */}
      {step === 5 && (
        <div className="space-y-4">
          <div className="bg-accent/10 border border-accent/30 rounded-md p-4 text-center">
            <div className="text-accent text-sm font-medium mb-1">Account connected successfully</div>
            <p className="text-xs text-base-400">
              Your {brokerLabel(broker)} account is ready to receive webhook alerts.
            </p>
          </div>

          <p className="text-xs text-base-400">
            {broker === 'tradovate'
              ? 'Upload a CSV from Tradovate to import historical trades, or skip to finish.'
              : 'Historical trades will be synced automatically in the background.'}
          </p>

          <div className="flex gap-3 pt-2">
            <button onClick={onDone} className="btn-primary">Done</button>
          </div>
        </div>
      )}
    </div>
  )
}
