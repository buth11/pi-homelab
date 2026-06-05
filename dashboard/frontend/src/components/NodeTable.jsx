import './NodeTable.css'

function ProgressBar({ value, max, colorFn }) {
  if (value == null || max == null || max === 0) return <span className="text-dim">—</span>
  const pct = Math.min(100, (value / max) * 100)
  const color = colorFn(pct)
  return (
    <div className="progress-row">
      <div className="progress-bar">
        <div className="progress-fill" style={{ width: `${pct}%`, background: color }} />
      </div>
      <span className="progress-label">{pct.toFixed(0)}%</span>
    </div>
  )
}

function cpuColor(pct) {
  if (pct > 85) return 'var(--red)'
  if (pct > 60) return 'var(--yellow)'
  return 'var(--green)'
}
function memColor(pct) {
  if (pct > 90) return 'var(--red)'
  if (pct > 70) return 'var(--yellow)'
  return 'var(--accent)'
}

function fmtCpu(m) {
  if (!m && m !== 0) return '—'
  return m >= 1000 ? `${(m / 1000).toFixed(2)}c` : `${m}m`
}
function fmtMem(mi) {
  if (!mi && mi !== 0) return '—'
  return mi >= 1024 ? `${(mi / 1024).toFixed(1)} GiB` : `${mi} MiB`
}

export default function NodeTable({ nodes }) {
  return (
    <div className="card">
      <div className="card-header">
        <span>Cluster Nodes</span>
        <span className="text-dim" style={{ fontSize: 12 }}>
          {nodes.filter(n => n.status === 'Ready').length}/{nodes.length} Ready
        </span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Node</th>
              <th>Status</th>
              <th>Role</th>
              <th>IP</th>
              <th>Version</th>
              <th>CPU</th>
              <th>Memory</th>
            </tr>
          </thead>
          <tbody>
            {nodes.length === 0 ? (
              <tr><td colSpan={7} className="empty-row">No nodes found</td></tr>
            ) : nodes.map(node => (
              <tr key={node.name}>
                <td>
                  <div className="node-name">
                    <span className={`node-dot ${node.status === 'Ready' ? 'dot-green' : 'dot-red'}`} />
                    <strong>{node.name}</strong>
                    {node.unschedulable && <span className="badge badge-yellow">cordoned</span>}
                  </div>
                </td>
                <td>
                  <span className={`badge ${node.status === 'Ready' ? 'badge-green' : 'badge-red'}`}>
                    {node.status}
                  </span>
                </td>
                <td>
                  <span className={`badge ${node.role === 'master' ? 'badge-purple' : 'badge-gray'}`}>
                    {node.role}
                  </span>
                </td>
                <td className="mono text-dim">{node.ip}</td>
                <td className="mono text-dim" style={{ fontSize: 12 }}>{node.version}</td>
                <td>
                  <div className="metric-cell">
                    <span className="metric-val">{fmtCpu(node.cpu_used_m)} / {fmtCpu(node.cpu_total_m)}</span>
                    <ProgressBar value={node.cpu_used_m} max={node.cpu_total_m} colorFn={cpuColor} />
                  </div>
                </td>
                <td>
                  <div className="metric-cell">
                    <span className="metric-val">{fmtMem(node.mem_used_mi)} / {fmtMem(node.mem_total_mi)}</span>
                    <ProgressBar value={node.mem_used_mi} max={node.mem_total_mi} colorFn={memColor} />
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
