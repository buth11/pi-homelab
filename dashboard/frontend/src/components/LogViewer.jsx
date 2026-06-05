import { useState, useEffect, useRef } from 'react'
import './LogViewer.css'

export default function LogViewer({ pods, initialPod }) {
  const [selected, setSelected] = useState(
    initialPod ? `${initialPod.namespace}|${initialPod.name}` : ''
  )
  const [lines, setLines] = useState(50)
  const [logs, setLogs] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [autoScroll, setAutoScroll] = useState(true)
  const logRef = useRef(null)

  const fetchLogs = async (sel = selected) => {
    if (!sel) return
    const [ns, name] = sel.split('|')
    setLoading(true)
    setError('')
    try {
      const r = await fetch(`/api/logs/${ns}/${name}?lines=${lines}`)
      if (!r.ok) {
        const e = await r.json()
        throw new Error(e.detail || r.statusText)
      }
      const d = await r.json()
      setLogs(d.logs || '')
    } catch (e) {
      setError(String(e))
      setLogs('')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (initialPod) {
      const sel = `${initialPod.namespace}|${initialPod.name}`
      setSelected(sel)
      fetchLogs(sel)
    }
  }, [initialPod])

  useEffect(() => {
    if (!selected) return
    fetchLogs()
  }, [selected, lines])

  useEffect(() => {
    if (autoScroll && logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight
    }
  }, [logs, autoScroll])

  const podName = selected ? selected.split('|')[1] : ''
  const logLines = logs.split('\n')

  function colorLine(line) {
    if (/error|fail|fatal|exception/i.test(line)) return 'log-error'
    if (/warn/i.test(line)) return 'log-warn'
    if (/info/i.test(line)) return 'log-info'
    if (/debug/i.test(line)) return 'log-debug'
    return ''
  }

  return (
    <div className="logviewer">
      <div className="log-toolbar">
        <select
          value={selected}
          onChange={e => setSelected(e.target.value)}
          style={{ flex: 1, maxWidth: 480 }}
        >
          <option value="">Select a pod…</option>
          {pods.map(p => (
            <option key={`${p.namespace}/${p.name}`} value={`${p.namespace}|${p.name}`}>
              [{p.namespace}] {p.name}
            </option>
          ))}
        </select>

        <select value={lines} onChange={e => setLines(Number(e.target.value))}>
          <option value={50}>50 lines</option>
          <option value={100}>100 lines</option>
          <option value={200}>200 lines</option>
          <option value={500}>500 lines</option>
        </select>

        <button className="btn-refresh-log" onClick={() => fetchLogs()} disabled={!selected || loading}>
          {loading ? <span className="spinner" style={{ width: 14, height: 14 }} /> : '↻'} Refresh
        </button>

        <label className="auto-scroll-label">
          <input type="checkbox" checked={autoScroll} onChange={e => setAutoScroll(e.target.checked)} />
          Auto-scroll
        </label>
      </div>

      {error && <div className="log-error-banner">⚠ {error}</div>}

      <div className="log-header">
        {podName && <span className="log-pod-name">{podName}</span>}
        {logs && <span className="log-count">{logLines.length} lines</span>}
      </div>

      <div className="log-body" ref={logRef}>
        {!selected && (
          <div className="log-empty">Select a pod above to view its logs.</div>
        )}
        {selected && !logs && !loading && !error && (
          <div className="log-empty">No logs available.</div>
        )}
        {logs && logLines.map((line, i) => (
          <div key={i} className={`log-line ${colorLine(line)}`}>
            <span className="log-num">{logLines.length - (logLines.length - i - 1)}</span>
            <span className="log-text">{line}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
