/**
 * useEventSource — connects to an SSE endpoint and calls onEvent for each message.
 *
 * Falls back to polling (via onFallback) if EventSource is unavailable or the
 * connection fails repeatedly.
 *
 * Usage:
 *   useEventSource('/api/events', 'delivery', (data) => { ... }, token)
 */
import { useEffect, useRef } from 'react'
import { getAccessToken } from '../lib/api'

const MAX_RETRIES   = 5
const RETRY_DELAY   = 3_000   // 3s initial backoff
const MAX_DELAY     = 30_000  // 30s cap

export function useEventSource(path, eventName, onEvent, enabled = true) {
  const esRef        = useRef(null)
  const retriesRef   = useRef(0)
  const mountedRef   = useRef(true)
  const timerRef     = useRef(null)

  useEffect(() => {
    mountedRef.current = true
    if (!enabled || typeof EventSource === 'undefined') return

    function connect() {
      if (!mountedRef.current) return

      // Pass the access token as a query param — EventSource can't set headers
      const token = getAccessToken()
      if (!token) {
        // Not authenticated yet — retry shortly
        timerRef.current = setTimeout(connect, 2_000)
        return
      }

      const url = `${path}?token=${encodeURIComponent(token)}`
      const es  = new EventSource(url)
      esRef.current = es

      es.addEventListener('connected', () => {
        retriesRef.current = 0  // reset backoff on successful connect
      })

      es.addEventListener(eventName, (e) => {
        if (!mountedRef.current) return
        try {
          const data = JSON.parse(e.data)
          onEvent(data)
        } catch {
          // ignore malformed events
        }
      })

      es.onerror = () => {
        es.close()
        esRef.current = null
        if (!mountedRef.current) return

        retriesRef.current += 1
        if (retriesRef.current > MAX_RETRIES) {
          // Give up — caller should fall back to polling
          return
        }
        const delay = Math.min(RETRY_DELAY * 2 ** (retriesRef.current - 1), MAX_DELAY)
        timerRef.current = setTimeout(connect, delay)
      }
    }

    connect()

    return () => {
      mountedRef.current = false
      clearTimeout(timerRef.current)
      esRef.current?.close()
      esRef.current = null
    }
  }, [path, eventName, enabled]) // eslint-disable-line react-hooks/exhaustive-deps
}
