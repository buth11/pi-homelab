import { useState, useEffect, useCallback, useMemo, useRef, Fragment } from 'react'
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
  const txt = y > 50 ? '50+ y' : `${y.toFixed(1)} y`
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
    d.manufactured && ['Manufactured', `${d.manufactured} (from ${d.manufactured_src})`],
    d.form_factor && ['Form factor', `${d.form_factor}${d.form_factor_src ? ` (from ${d.form_factor_src})` : ''}`],
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

// ---------------------------------------------------------------------------
// Columns. value() feeds sorting, fmt() the filter list (same text as the
// cell), render() the cell itself when it is more than text.
// ---------------------------------------------------------------------------

const fmtTbwLeft = v => (v == null ? '—' : `${v.toFixed(1)} TB`)
const fmtTemp = v => (v == null ? '—' : `${v}°C`)
const fmtRate = v => (v == null ? '—' : v.toFixed(1))

const COLUMNS = [
  { key: 'host', label: 'Host', value: d => d.host, cls: 'nowrap', render: d => <strong>{d.host}</strong> },
  { key: 'dev', label: 'Device', value: d => d.dev, cls: 'mono text-dim nowrap' },
  { key: 'model', label: 'Model', value: d => d.model },
  { key: 'kind', label: 'Type', value: d => d.kind,
    render: d => <span className="badge badge-gray">{d.kind}</span> },
  { key: 'form_factor', label: 'Form factor', value: d => d.form_factor, cls: 'nowrap',
    title: 'Physical format: 3.5", 2.5", M.2, microSD…',
    render: d => <span title={d.form_factor_src ? `From ${d.form_factor_src}` : undefined}>{d.form_factor ?? '—'}</span> },
  { key: 'manufactured', label: 'Made', value: d => d.manufactured, cls: 'mono nowrap',
    title: 'Production date (year-month). Only SD/microSD cards report it (CID register); SATA/NVMe/HDD SMART has no production date (see the drive label).',
    render: d => <span title={d.manufactured_src ? `From ${d.manufactured_src}` : 'Not reported by SMART for this drive type'}>{d.manufactured ?? '—'}</span> },
  { key: 'capacity', label: 'Size', num: true, value: d => d.capacity, fmt: fmtSize, cls: 'mono' },
  { key: 'poh', label: 'Hours', num: true, value: d => d.poh, cls: 'mono' },
  { key: 'written', label: 'Written', num: true, value: d => d.written, fmt: fmtBytes, cls: 'mono' },
  { key: 'wear_pct', label: 'Wear', num: true, value: d => d.wear_pct, fmt: fmtPct,
    title: "Higher of: % of rated TBW and the drive's own counter (HDD: load cycles)",
    render: d => <Wear pct={d.wear_pct} src={d.wear_src} /> },
  { key: 'tbw_left_tb', label: 'TBW left', num: true, value: d => d.tbw_left_tb, fmt: fmtTbwLeft,
    title: 'Rated TBW minus bytes written', cls: 'mono' },
  { key: 'temp', label: 'Temp', num: true, value: d => d.temp, fmt: fmtTemp, cls: 'mono' },
  { key: 'gb_per_day', label: 'GB/day', num: true, value: d => d.gb_per_day, fmt: fmtRate,
    title: 'Write rate per day', cls: 'mono',
    render: d => (<>
      {fmtRate(d.gb_per_day)}
      {d.rate_src === 'lifetime average' && <span className="text-dim" title="Lifetime average per power-on day (no history yet)">*</span>}
    </>) },
  { key: 'years_left', label: 'Life left', num: true, value: d => d.years_left,
    fmt: (_, d) => fmtYears(d.years_left, d.years_confident), cls: 'mono',
    render: d => <span title={d.years_src ? `Projection from: ${d.years_src}` : undefined}>{fmtYears(d.years_left, d.years_confident)}</span> },
  { key: 'level', label: 'Status', value: d => d.level, sortValue: d => LEVEL_ORDER[d.level] ?? 9,
    render: d => <span className={`badge ${LEVEL_BADGE[d.level] ?? 'badge-gray'}`}>{d.level}</span> },
]
const COL = Object.fromEntries(COLUMNS.map(c => [c.key, c]))

function cellText(col, d) {
  const v = col.value(d)
  if (col.fmt) return String(col.fmt(v, d))
  return v == null || v === '' ? '—' : String(v)
}

function compare(a, b) {
  // nulls always last, whatever the direction (applied by the caller)
  if (typeof a === 'number' && typeof b === 'number') return a - b
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' })
}

function defaultOrder(a, b) {
  return (LEVEL_ORDER[a.level] ?? 9) - (LEVEL_ORDER[b.level] ?? 9) ||
    compare(a.host, b.host) || compare(a.dev, b.dev)
}

function SortIcon({ active, dir }) {
  if (!active) return <span className="sort-icon sort-none">⇅</span>
  return dir === 1 ? <span className="sort-icon sort-asc">↑</span> : <span className="sort-icon sort-desc">↓</span>
}

function loadPref(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback } catch { return fallback }
}

// Excel-like column menu: sort, search, checkbox list of values with counts.
function ColumnMenu({ col, anchor, rows, selected, sort, onSort, onChange, onClose }) {
  const ref = useRef(null)
  const [q, setQ] = useState('')

  useEffect(() => {
    const away = e => { if (ref.current && !ref.current.contains(e.target)) onClose() }
    const esc = e => { if (e.key === 'Escape') onClose() }
    document.addEventListener('mousedown', away)
    document.addEventListener('keydown', esc)
    window.addEventListener('resize', onClose)
    return () => {
      document.removeEventListener('mousedown', away)
      document.removeEventListener('keydown', esc)
      window.removeEventListener('resize', onClose)
    }
  }, [onClose])

  // Distinct values (as displayed) with counts, ordered like the column sorts.
  const values = useMemo(() => {
    const m = new Map()
    for (const d of rows) {
      const t = cellText(col, d)
      const e = m.get(t) || { text: t, count: 0, v: (col.sortValue || col.value)(d) }
      e.count++
      m.set(t, e)
    }
    return [...m.values()].sort((a, b) =>
      (a.v == null) - (b.v == null) || (a.v == null ? 0 : compare(a.v, b.v)))
  }, [rows, col])

  const shown = values.filter(v => v.text.toLowerCase().includes(q.toLowerCase()))
  const isOn = t => !selected || selected.includes(t)
  const allShownOn = shown.every(v => isOn(v.text))

  function set(next) {
    // all values selected = no filter
    onChange(values.every(v => next.includes(v.text)) ? null : next)
  }
  function toggle(t) {
    const cur = selected ?? values.map(v => v.text)
    set(cur.includes(t) ? cur.filter(x => x !== t) : [...cur, t])
  }
  function toggleShown() {
    const cur = selected ?? values.map(v => v.text)
    const names = shown.map(v => v.text)
    set(allShownOn ? cur.filter(t => !names.includes(t)) : [...new Set([...cur, ...names])])
  }

  const left = Math.max(8, Math.min(anchor.left, window.innerWidth - 268))
  return (
    <div className="col-menu" ref={ref} style={{ top: anchor.bottom + 4, left }} onClick={e => e.stopPropagation()}>
      <button className={`col-menu-item ${sort.key === col.key && sort.dir === 1 ? 'active' : ''}`} onClick={() => onSort(1)}>
        ↑ Sort {col.num ? 'smallest to largest' : 'A → Z'}
      </button>
      <button className={`col-menu-item ${sort.key === col.key && sort.dir === -1 ? 'active' : ''}`} onClick={() => onSort(-1)}>
        ↓ Sort {col.num ? 'largest to smallest' : 'Z → A'}
      </button>
      <div className="col-menu-sep" />
      <input className="col-menu-search" placeholder="Search…" value={q} onChange={e => setQ(e.target.value)} autoFocus />
      <label className="col-menu-row col-menu-all">
        <input type="checkbox" checked={allShownOn} onChange={toggleShown} />
        <span>(Select all{q ? ' shown' : ''})</span>
      </label>
      <div className="col-menu-list">
        {shown.map(v => (
          <label key={v.text} className="col-menu-row">
            <input type="checkbox" checked={isOn(v.text)} onChange={() => toggle(v.text)} />
            <span className="col-menu-value">{v.text}</span>
            <span className="col-menu-count">{v.count}</span>
          </label>
        ))}
        {shown.length === 0 && <div className="text-dim col-menu-row">No matches</div>}
      </div>
      <div className="col-menu-actions">
        <button className="col-menu-btn" onClick={() => onChange(null)} disabled={!selected}>Clear filter</button>
        <button className="col-menu-btn col-menu-btn-primary" onClick={onClose}>Close</button>
      </div>
    </div>
  )
}

export default function DiskTable() {
  const [data, setData] = useState(null)
  const [open, setOpen] = useState(null)
  const [fetchError, setFetchError] = useState(null)
  const [sort, setSort] = useState(() => loadPref('disks.sort', { key: null, dir: 1 }))
  const [filters, setFilters] = useState(() => loadPref('disks.filters', {}))  // key -> allowed texts
  const [menu, setMenu] = useState(null)  // { key, anchor }

  useEffect(() => { localStorage.setItem('disks.sort', JSON.stringify(sort)) }, [sort])
  useEffect(() => { localStorage.setItem('disks.filters', JSON.stringify(filters)) }, [filters])

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

  function toggleSort(key) {
    setSort(s => {
      if (s.key !== key) return { key, dir: 1 }
      if (s.dir === 1) return { key, dir: -1 }
      return { key: null, dir: 1 }
    })
  }

  function setFilter(key, allowed) {
    setFilters(f => {
      const next = { ...f }
      if (allowed) next[key] = allowed
      else delete next[key]
      return next
    })
  }

  const closeMenu = useCallback(() => setMenu(null), [])

  const last = data?.last
  const all = last?.disks ?? []
  const counts = all.reduce((c, d) => ({ ...c, [d.level]: (c[d.level] ?? 0) + 1 }), {})
  const anyLifetimeAvg = all.some(d => d.rate_src === 'lifetime average')

  const passes = (d, except) => Object.entries(filters).every(([k, allowed]) =>
    k === except || !COL[k] || allowed.includes(cellText(COL[k], d)))

  const disks = useMemo(() => {
    const rows = all.filter(d => passes(d))
    const col = sort.key && COL[sort.key]
    if (!col) return rows.sort(defaultOrder)
    const get = col.sortValue || col.value
    return rows.sort((a, b) => {
      const av = get(a), bv = get(b)
      if (av == null || bv == null) return (av == null) - (bv == null) || defaultOrder(a, b)
      return sort.dir * compare(av, bv) || defaultOrder(a, b)
    })
  }, [all, filters, sort])  // eslint-disable-line react-hooks/exhaustive-deps

  const nFilters = Object.keys(filters).filter(k => COL[k]).length

  return (
    <div className="card">
      <div className="card-header">
        <span>Disk health (SMART)</span>
        <div className="disk-header-right">
          {last && (
            <span className="text-dim" style={{ fontSize: 12 }} title={new Date(last.time).toLocaleString()}>
              Last scan {ago(last.time)} · {nFilters ? `${disks.length} of ${all.length}` : all.length} disks · {last.hosts} hosts
            </span>
          )}
          {nFilters > 0 && (
            <button className="disk-clear-btn" onClick={() => setFilters({})}>
              ✕ Clear {nFilters} filter{nFilters > 1 ? 's' : ''}
            </button>
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
              {COLUMNS.map(c => (
                <th key={c.key} title={c.title}
                    className={`th-sortable ${sort.key === c.key ? 'th-active' : ''}`}>
                  <span className="th-inner">
                    <span className="th-label" onClick={() => toggleSort(c.key)}>
                      {c.label} <SortIcon active={sort.key === c.key} dir={sort.dir} />
                    </span>
                    <button className={`th-filter ${filters[c.key] ? 'th-filter-on' : ''}`}
                            title={filters[c.key] ? 'Filtered: click to change' : 'Sort and filter'}
                            onClick={e => {
                              const r = e.currentTarget.getBoundingClientRect()
                              setMenu(m => (m?.key === c.key ? null : { key: c.key, anchor: { left: r.left, bottom: r.bottom } }))
                            }}>
                      {filters[c.key] ? '⧩' : '▾'}
                    </button>
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {!last ? (
              <tr><td colSpan={COLUMNS.length} className="empty-row">
                {running ? 'First scan in progress…' : 'No scan yet. Click “Scan now”.'}
              </td></tr>
            ) : disks.length === 0 ? (
              <tr><td colSpan={COLUMNS.length} className="empty-row">
                No disk matches the filters. <button className="disk-clear-btn" onClick={() => setFilters({})}>Clear filters</button>
              </td></tr>
            ) : disks.map(d => {
              const key = `${d.host}${d.dev}`
              const dim = d.level === 'SKIP' || d.level === 'INFO'
              return (
                <Fragment key={key}>
                  <tr className={`disk-row ${dim ? 'disk-row-dim' : ''} ${open === key ? 'disk-row-open' : ''}`}
                      onClick={() => setOpen(open === key ? null : key)}>
                    {COLUMNS.map(c => (
                      <td key={c.key} className={c.cls}>{c.render ? c.render(d) : cellText(c, d)}</td>
                    ))}
                  </tr>
                  {open === key && (
                    <tr className="disk-details-row"><td colSpan={COLUMNS.length}><DiskDetails d={d} /></td></tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>

      {menu && COL[menu.key] && (
        <ColumnMenu
          col={COL[menu.key]}
          anchor={menu.anchor}
          rows={all.filter(d => passes(d, menu.key))}
          selected={filters[menu.key] ?? null}
          sort={sort}
          onSort={dir => setSort({ key: menu.key, dir })}
          onChange={allowed => setFilter(menu.key, allowed)}
          onClose={closeMenu}
        />
      )}

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
      <div className="disk-footnote text-dim">
        {anyLifetimeAvg && '* / ~ = lifetime average, no history yet; never raises an alert. Real trends start from a scan at least a day after the first. '}
        Click a column name to sort, ▾ to sort and filter, a row for details and history.
      </div>
    </div>
  )
}
