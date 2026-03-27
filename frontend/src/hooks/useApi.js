import { useState, useEffect, useCallback, useRef } from 'react'

/**
 * Generic data fetching hook.
 * Usage: const { data, loading, error, refetch } = useApi(() => orders.list())
 */
export function useApi(fetcher, deps = []) {
  const [data, setData]       = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError]     = useState(null)
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const fetch = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const result = await fetcherRef.current()
      setData(result)
    } catch (e) {
      setError(e.detail || e.message || 'Unknown error')
    } finally {
      setLoading(false)
    }
  }, deps) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { fetch() }, [fetch])

  return { data, loading, error, refetch: fetch }
}

/**
 * Polling hook — refetches at `interval` ms while mounted.
 * Pass { paused: true } in options to temporarily stop polling.
 */
export function usePolling(fetcher, interval = 30_000, deps = [], options = {}) {
  const result = useApi(fetcher, deps)
  const paused = options.paused ?? false
  useEffect(() => {
    if (paused) return
    const id = setInterval(result.refetch, interval)
    return () => clearInterval(id)
  }, [interval, result.refetch, paused])
  return result
}
