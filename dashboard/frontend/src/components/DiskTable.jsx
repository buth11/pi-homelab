import { useState, useEffect, useCallback, Fragment } from 'react'
import './NodeTable.css'
import './DiskTable.css'

// SMART wear report (backend: dashboard/backend/smart.py, logic shared with
// smart-report/smart_report.py). Not part of the 30 s auto-refresh: a scan
// SSHes to every host, so it only runs when asked.

const LEVEL_BADGE = {
  OK: 'badge-green', WARN: 'badge-yellow', CRIT: 'badge-red',
  SKIP: 'badge-gray', INFO: 'badge-gray',
}
const LEVEL_ORDER = { CRIT: 0, WARN: 1, OK: 2, SKIP: 3, INFO: 4 }

function fmtBytes(n) {
  if (n == null) return '—'
  return n >= 1e12 ? `${(n / 1e12).toFixed(2)} TB` : `${(n / 1e9).toFixed(0)} GB`
}
function fmtSize(n) {
  if (!n) return '—'
  return n >= 1e12 ? `${(n / 1e12).toFixed(1)} TB` : `${(n / 1e9).toFixed(0)} GB`
}
function fmtYears(y, confident) {
  if (y == null) return '—'
  const txt = y > 50 ? '> 50 y' : `${y.toFixed(1)} y`
  return confident ? txt : `~${txt}`  // ~ = from lifetime average, no alert yet
}
function wearColor(pct) {
  if (pct >= 90) return 'var(--red)'
  if (pct >= 70) return 'var(--yellow)'
  return 'var(--green)'
}
function ago(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 90) return 'just now'
  if (s < 5400) return `${Math.round(s / 60)} min ago`
  if (s < 172800) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / 86400)} days ago`
}

function fmtPct(p) {
  if (p == null) return '—'
  return p >= 10 ? `${p.toFixed(0)}%` : `${p.toFixed(1)}%`
}

function Wear({ pct, src }) {
  if (pct == null) return <span className="text-dim">—</span>
  return (
    <div className="progress-row" title={src ? `From ${src}` : undefined}>
      <div className="progress-bar disk-wear-bar">
        <div className="progress-fill" style={{ width: `${Math.min(pct, 100)}%`, background: wearColor(pct) }} />
      </div>
      <span className="progress-label">{fmtPct(pct)}</span>
    </div>
  )
}

function DiskHistory({ serial }) {
  const [rows, setRows] = useState(null)
  useEffect(() => {
    fetch(`/api/smart/history?serial=${encodeURIComponent(serial)}`)
      .then(r => r.json()).then(setRows).catch(() => setRows([]))
  }, [serial])
  if (rows === null) return <div className="text-dim">Loading history…</div>
  if (rows.length === 0) return <div className="text-dim">No history yet.</div>
  return (
    <table className="disk-history">
      <thead><tr><th>Snapshot</th><th>Written</th><th>Δ since previous</th><th>Wear</th><th>Hours</th><th>Temp</th></tr></thead>
      <tbody>
        {[...rows].reverse().slice(0, 20).map((r, i, arr) => {
          const prev = arr[i + 1]
          const delta = prev && r.written != null && prev.written != null ? r.written - prev.written : null
          return (
            <tr key={r.ts}>
              <td className="mono">{new Date(r.ts).toLocaleString()}</td>
              <td className="mono">{fmtBytes(r.written)}</td>
              <td className="mono text-dim">{delta == null ? '—' : `+${fmtBytes(delta)}`}</td>
              <td className="mono">{r.used_pct == null ? '—' : `${r.used_pct}%`}</td>
              <td className="mono">{r.poh ?? '—'}</td>
              <td className="mono">{r.temp == null ? '—' : `${r.temp}°C`}</td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function DiskDetails({ d }) {
  const facts = [
    d.serial && ['Serial', d.serial],
    d.firmware && ['Firmware', d.firmware],
    d.tbw && ['Rated endurance', `${d.tbw} TBW · ${fmtPct(d.tbw_pct)} written · ${d.tbw_left_tb?.toFixed(1)} TB left`],
    d.kind !== 'HDD' && d.used_pct != null && ["Drive's own wear counter", `${d.used_pct}%${d.pct_per_year ? ` (+${d.pct_per_year.toFixed(1)}%/year)` : ''}`],
    d.wear_src && ['Wear column uses', d.wear_src],
    d.read != null && ['Read (lifetime)', fmtBytes(d.read)],
    d.load_cycles != null && ['Load/unload cycles', `${d.load_cycles}${d.load_cycles_rating ? ` of ${d.load_cycles_rating.toLocaleString()}` : ''}${d.load_cycles_per_day != null ? ` · ${d.load_cycles_per_day.toFixed(1)}/day` : ''}`],
    d.start_stop != null && ['Start/stop count', `${d.start_stop}${d.starts_per_day != null ? ` · ${d.starts_per_day.toFixed(1)}/day` : ''}`],
    d.workload_tb_year != null && ['Workload (read + write)', `${d.workload_tb_year.toFixed(1)} TB/year${d.workload_rating ? ` of ${d.workload_rating} rated` : ''}`],
    d.unsafe_shutdowns != null && ['Unsafe shutdowns', d.unsafe_shutdowns],
    d.rate_src && ['Write rate from', d.rate_src],
    d.years_src && ['Projection from', `${d.years_src}${d.years_confident ? '' : ' (rough, no alerts)'}`],
  ].filter(Boolean)
  return (
    <div className="disk-details">
      {d.issues?.length > 0 && (
        <ul className="disk-issues">
          {d.issues.map((i, k) => (
            <li key={k} className={i.startsWith('CRIT') ? 'issue-crit' : i.startsWith('WARN') ? 'issue-warn' : ''}>{i}</li>
          ))}
        </ul>
      )}
      {facts.length > 0 && (
        <dl className="disk-facts">
          {facts.map(([k, v]) => <Fragment key={k}><dt>{k}</dt><dd className="mono">{v}</dd></Fragment>)}
        </dl>
      )}
      {d.serial && <DiskHistory serial={d.serial} />}
    </div>
  )
}

export default function DiskTable() {
  const [data, setData] = useState(null)
  const [open, setOpen] = useState(null)
  const [fetchError, setFetchError] = useState(null)

  const load = useCallback(async () => {
    try {
      const r = await fetch('/api/smart')
      setData(await r.json())
      setFetchError(null)
    } catch (e) {
      setFetchError(String(e))
    }
  }, [])

  useEffect(() => { load() }, [load])

  // Poll only while a scan is running.
  const running = data?.state === 'running'
  useEffect(() => {
    if (!running) return
    const id = setInterval(load, 2000)
    return () => clearInterval(id)
  }, [running, load])

  async function scan() {
    await fetch('/api/smart/scan', { method: 'POST' })
    load()
  }

  const last = data?.last
  const disks = [...(last?.disks ?? [])].sort((a, b) =>
    (LEVEL_ORDER[a.level] ?? 9) - (LEVEL_ORDER[b.level] ?? 9) ||
    a.host.localeCompare(b.host) || a.dev.localeCompare(b.dev))
  const counts = disks.reduce((c, d) => ({ ...c, [d.level]: (c[d.level] ?? 0) + 1 }), {})
  const anyLifetimeAvg = disks.some(d => d.rate_src === 'lifetime average')

  return (
    <div className="card">
      <div className="card-header">
        <span>Disk health (SMART)</span>
        <div className="disk-header-right">
          {last && (
            <span className="text-dim" style={{ fontSize: 12 }} title={new Date(last.time).toLocaleString()}>
              Last scan {ago(last.time)} · {disks.length} disks · {last.hosts} hosts
            </span>
          )}
          {['CRIT', 'WARN', 'OK'].map(l => counts[l] ? (
            <span key={l} className={`badge ${LEVEL_BADGE[l]}`}>{counts[l]} {l}</span>
          ) : null)}
          <button className="disk-scan-btn" onClick={scan} disabled={running}>
            {running ? <><span className="spinner disk-spinner" /> Scanning…</> : '↻ Scan now'}
          </button>
        </div>
      </div>

      {(data?.state === 'error' || fetchError) && (
        <div className="disk-banner disk-banner-error">Scan failed: {data?.error || fetchError}</div>
      )}

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Host</th><th>Device</th><th>Model</th><th>Type</th><th>Size</th>
              <th>Hours</th><th>Written</th><th title="Higher of: % of rated TBW and the drive's own counter (HDD: load cycles)">Wear</th>
              <th title="Rated TBW minus bytes written">TBW left</th><th>Temp</th>
              <th title="Write rate per day">GB/day</th><th>Life left</th><th>Status</th>
            </tr>
          </thead>
          <tbody>
            {!last ? (
              <tr><td colSpan={13} className="empty-row">
                {running ? 'First scan in progress…' : 'No scan yet. Click “Scan now”.'}
              </td></tr>
            ) : disks.map(d => {
              const key = `${d.host}${d.dev}`
              const dim = d.level === 'SKIP' || d.level === 'INFO'
              return (
                <Fragment key={key}>
                  <tr className={`disk-row ${dim ? 'disk-row-dim' : ''} ${open === key ? 'disk-row-open' : ''}`}
                      onClick={() => setOpen(open === key ? null : key)}>
                    <td><strong>{d.host}</strong></td>
                    <td className="mono text-dim">{d.dev}</td>
                    <td>{d.model}</td>
                    <td><span className="badge badge-gray">{d.kind}</span></td>
                    <td className="mono">{fmtSize(d.capacity)}</td>
                    <td className="mono">{d.poh ?? '—'}</td>
                    <td className="mono">{fmtBytes(d.written)}</td>
                    <td><Wear pct={d.wear_pct} src={d.wear_src} /></td>
                    <td className="mono">{d.tbw_left_tb == null ? '—' : `${d.tbw_left_tb.toFixed(1)} TB`}</td>
                    <td className="mono">{d.temp == null ? '—' : `${d.temp}°C`}</td>
                    <td className="mono">
                      {d.gb_per_day == null ? '—' : d.gb_per_day.toFixed(1)}
                      {d.rate_src === 'lifetime average' && <span className="text-dim" title="Lifetime average per power-on day (no history yet)">*</span>}
                    </td>
                    <td className="mono" title={d.years_src ? `Projection from: ${d.years_src}` : undefined}>{fmtYears(d.years_left, d.years_confident)}</td>
                    <td><span className={`badge ${LEVEL_BADGE[d.level] ?? 'badge-gray'}`}>{d.level}</span></td>
                  </tr>
                  {open === key && (
                    <tr className="disk-details-row"><td colSpan={13}><DiskDetails d={d} /></td></tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>

      {last?.host_errors?.length > 0 && (
        <div className="disk-banner disk-banner-warn">
          {last.host_errors.map(([h, e], i) => <div key={i}><strong>{h}</strong>: {e}</div>)}
        </div>
      )}
      {data?.backup && (
        <div className={`disk-footnote ${data.backup.ok ? 'text-dim' : 'disk-backup-warn'}`}>
          {data.backup.ok
            ? `History backed up to MinIO ${ago(data.backup.at)}.`
            : `MinIO backup failed (${data.backup.error})${data.backup.last_ok ? `; last good copy ${ago(data.backup.last_ok)}` : ''}. Retried on the next scan.`}
        </div>
      )}
      {anyLifetimeAvg && (
        <div className="disk-footnote text-dim">
          * / ~ = lifetime average, no history yet; never raises an alert. Real trends start from a scan at least a day after the first.
          Click a row for details and history.
        </div>
      )}
    </div>
  )
}
