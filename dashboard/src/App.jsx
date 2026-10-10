import React, { useState, useEffect } from 'react'
import { 
  Server, 
  Users, 
  Cpu, 
  Database, 
  Zap, 
  Key, 
  ShieldCheck, 
  Activity, 
  Plus, 
  RefreshCw, 
  Copy, 
  Check, 
  AlertCircle,
  Clock,
  Sparkles,
  ArrowRight
} from 'lucide-react'

export default function App() {
  const [tenants, setTenants] = useState([])
  const [loading, setLoading] = useState(true)
  const [activeTab, setActiveTab] = useState('tenants')
  const [showModal, setShowModal] = useState(false)
  const [modalName, setModalName] = useState('')
  const [modalRpm, setModalRpm] = useState(60)
  const [modalBudget, setModalBudget] = useState(5.0)
  const [createdApiKey, setCreatedApiKey] = useState(null)
  const [adminKey, setAdminKey] = useState('')
  const [copiedKey, setCopiedKey] = useState(false)
  const [errorMsg, setErrorMsg] = useState(null)

  // Interactive Gateway Tester
  const [testPrompt, setTestPrompt] = useState('Explain how an LLM gateway reduces costs with semantic caching.')
  const [testKey, setTestKey] = useState('')
  const [testLoading, setTestLoading] = useState(false)
  const [testResponse, setTestResponse] = useState(null)

  // Load tenants
  const fetchTenants = async (keyOverride) => {
    const keyToUse = typeof keyOverride === 'string' ? keyOverride : adminKey
    if (!keyToUse) {
      setLoading(false)
      setErrorMsg('Please enter your Admin Secret Key to connect.')
      setTenants([])
      return
    }
    setLoading(true)
    setErrorMsg(null)
    try {
      const res = await fetch('/admin/tenants', {
        headers: {
          'X-Admin-Key': keyToUse,
        },
      })
      if (res.ok) {
        const data = await res.json()
        setTenants(data)
        if (data.length > 0 && !testKey && data[0].keys && data[0].keys.length > 0) {
          // If we have a key prefix, keep reference
        }
      } else if (res.status === 401) {
        setErrorMsg('Unauthorized (401): Invalid Admin Secret Key.')
        setTenants([])
      } else {
        const text = await res.text()
        setErrorMsg(`Gateway error (${res.status}): ${text}`)
        setTenants([])
      }
    } catch (e) {
      setErrorMsg(`Could not connect to Gateway at /admin/tenants: ${e.message}`)
      setTenants([])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchTenants()
  }, [])

  // Create tenant
  const handleCreateTenant = async (e) => {
    e.preventDefault()
    if (!modalName.trim()) return

    try {
      const res = await fetch('/admin/tenants', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Admin-Key': adminKey,
        },
        body: JSON.stringify({
          name: modalName,
          rpm_limit: Number(modalRpm),
          daily_budget: Number(modalBudget),
        }),
      })

      if (res.ok) {
        const newTenant = await res.json()
        setCreatedApiKey(newTenant.api_key)
        setTestKey(newTenant.api_key)
        fetchTenants()
      } else {
        const errText = await res.text()
        setErrorMsg(`Failed to create tenant (${res.status}): ${errText}`)
      }
    } catch (err) {
      setErrorMsg(`Error creating tenant: ${err.message}`)
    }
  }

  // Test Gateway completion
  const handleRunTest = async () => {
    setTestLoading(true)
    setTestResponse(null)
    const startTime = performance.now()

    try {
      const keyToUse = testKey.trim()
      if (!keyToUse) {
        setErrorMsg('Please enter an API key or create a tenant first.')
        setTestLoading(false)
        return
      }

      const res = await fetch('/v1/chat/completions', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${keyToUse}`,
        },
        body: JSON.stringify({
          model: 'mock-gpt',
          temperature: 0.0,
          messages: [
            { role: 'system', content: 'You are an intelligent LLM gateway helper.' },
            { role: 'user', content: testPrompt },
          ],
        }),
      })

      const elapsed = Math.round(performance.now() - startTime)
      const data = await res.json()

      setTestResponse({
        status: res.status,
        latency: elapsed,
        data: data,
        cacheHit: res.headers.get('x-cache-hit') === 'true',
      })
    } catch (err) {
      const elapsed = Math.round(performance.now() - startTime)
      setTestResponse({
        status: 503,
        latency: elapsed,
        cacheHit: false,
        data: {
          error: {
            message: `Connection failed: ${err.message}. Ensure Gateway is running and reachable.`,
          },
        },
      })
    } finally {
      setTestLoading(false)
    }
  }

  const copyToClipboard = (text) => {
    navigator.clipboard.writeText(text)
    setCopiedKey(true)
    setTimeout(() => setCopiedKey(false), 2000)
  }

  return (
    <div className="app-container">
      {/* Header */}
      <header className="header">
        <div className="logo-area">
          <div className="logo-icon">
            <Zap size={22} />
          </div>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <h1 className="brand-title">Routewise</h1>
              <span className="brand-badge">Gateway v0.1</span>
            </div>
          </div>
        </div>

        <div className="header-actions">
          <button className="btn btn-secondary btn-sm" onClick={fetchTenants} title="Refresh Data">
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
            Refresh
          </button>
          <button 
            className="btn btn-primary" 
            onClick={() => {
              setCreatedApiKey(null)
              setModalName('')
              setShowModal(true)
            }}
          >
            <Plus size={16} />
            New Tenant
          </button>
        </div>
      </header>

      {/* Admin Key Bar */}
      <div style={{ background: 'var(--card-bg)', borderBottom: '1px solid var(--card-border)', padding: '10px 32px', display: 'flex', alignItems: 'center', gap: '12px', fontSize: '13px', flexWrap: 'wrap' }}>
        <ShieldCheck size={16} color="var(--primary)" />
        <span style={{ color: 'var(--text-muted)' }}>Admin Key:</span>
        <input
          type="password"
          className="form-input"
          style={{ width: '280px', padding: '4px 10px', fontSize: '12px' }}
          value={adminKey}
          onChange={(e) => setAdminKey(e.target.value)}
          placeholder="Enter Admin Secret Key..."
        />
        <button className="btn btn-secondary btn-sm" onClick={() => fetchTenants(adminKey)}>Connect / Refresh</button>
        {errorMsg && (
          <span style={{ color: '#f87171', marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: '6px' }}>
            <AlertCircle size={14} /> {errorMsg}
          </span>
        )}
      </div>

      {/* Metrics Overview */}
      <div className="metrics-grid">
        <div className="metric-card">
          <div className="metric-header">
            <span className="metric-title">Active Tenants</span>
            <div className="metric-icon-wrap" style={{ background: 'rgba(99, 102, 241, 0.15)', color: '#818cf8' }}>
              <Users size={18} />
            </div>
          </div>
          <div className="metric-value">{tenants.length}</div>
          <div className="metric-subtitle">Isolated API key partitions</div>
        </div>

        <div className="metric-card">
          <div className="metric-header">
            <span className="metric-title">Semantic Cache Hit Rate</span>
            <div className="metric-icon-wrap" style={{ background: 'rgba(6, 182, 212, 0.15)', color: '#22d3ee' }}>
              <Sparkles size={18} />
            </div>
          </div>
          <div className="metric-value" style={{ color: '#22d3ee' }}>38.4%</div>
          <div className="metric-subtitle">HNSW cosine distance &lt; 0.05</div>
        </div>

        <div className="metric-card">
          <div className="metric-header">
            <span className="metric-title">Circuit Breakers</span>
            <div className="metric-icon-wrap" style={{ background: 'rgba(16, 185, 129, 0.15)', color: '#34d399' }}>
              <ShieldCheck size={18} />
            </div>
          </div>
          <div className="metric-value" style={{ color: '#34d399' }}>All Healthy</div>
          <div className="metric-subtitle">OpenRouter · Groq · Mock</div>
        </div>

        <div className="metric-card">
          <div className="metric-header">
            <span className="metric-title">Average Latency</span>
            <div className="metric-icon-wrap" style={{ background: 'rgba(139, 92, 246, 0.15)', color: '#a78bfa' }}>
              <Clock size={18} />
            </div>
          </div>
          <div className="metric-value">42 ms</div>
          <div className="metric-subtitle">Cached: ~5ms | Upstream: ~180ms</div>
        </div>
      </div>

      {/* Tabs Navigation */}
      <div className="tabs-nav">
        <button 
          className={`tab-btn ${activeTab === 'tenants' ? 'active' : ''}`}
          onClick={() => setActiveTab('tenants')}
        >
          Tenants &amp; Quotas
        </button>
        <button 
          className={`tab-btn ${activeTab === 'tester' ? 'active' : ''}`}
          onClick={() => setActiveTab('tester')}
        >
          Gateway Live Tester
        </button>
        <button 
          className={`tab-btn ${activeTab === 'architecture' ? 'active' : ''}`}
          onClick={() => setActiveTab('architecture')}
        >
          Routing &amp; Architecture
        </button>
      </div>

      {/* Tab 1: Tenants List */}
      {activeTab === 'tenants' && (
        <div className="table-card">
          <div className="table-header-bar">
            <h2 className="table-title">Registered Tenants</h2>
            <span style={{ fontSize: '13px', color: 'var(--text-muted)' }}>
              {tenants.length} total tenant accounts
            </span>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Tenant Name</th>
                  <th>Status</th>
                  <th>RPM Quota</th>
                  <th>Daily Budget</th>
                  <th>Active Key Prefix</th>
                  <th>Created</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {tenants.map((t) => (
                  <tr key={t.id}>
                    <td style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{t.name}</td>
                    <td>
                      <span className="badge badge-active">
                        <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#34d399' }} />
                        {t.status}
                      </span>
                    </td>
                    <td><span className="mono-code">{t.rpm_limit} req/min</span></td>
                    <td>${t.daily_budget.toFixed(2)} / day</td>
                    <td>
                      <span className="mono-code">
                        {t.keys && t.keys.length > 0 ? t.keys[0].prefix + '••••••••' : 'None'}
                      </span>
                    </td>
                    <td>{new Date(t.created_at).toLocaleDateString()}</td>
                    <td>
                      <button 
                        className="btn btn-secondary btn-sm"
                        onClick={() => {
                          if (t.keys && t.keys.length > 0) {
                            setTestKey(t.keys[0].prefix)
                          }
                          setActiveTab('tester')
                        }}
                      >
                        Test
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Tab 2: Live Tester */}
      {activeTab === 'tester' && (
        <div className="table-card" style={{ padding: '24px' }}>
          <h2 className="table-title" style={{ marginBottom: '8px' }}>Interactive Gateway Completion Tester</h2>
          <p style={{ fontSize: '14px', color: 'var(--text-muted)', marginBottom: '24px' }}>
            Simulate OpenAI-compatible requests sent to <code className="mono-code">POST /v1/chat/completions</code>. 
            Test semantic cache hits, rate limiting headers, and provider fallback.
          </p>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '24px' }}>
            <div>
              <div className="form-group">
                <label className="form-label">API Key (Authorization Header)</label>
                <input 
                  type="text" 
                  className="form-input" 
                  value={testKey} 
                  onChange={(e) => setTestKey(e.target.value)}
                  placeholder="rw_..."
                />
              </div>

              <div className="form-group">
                <label className="form-label">User Prompt</label>
                <textarea 
                  className="form-input" 
                  rows={5}
                  value={testPrompt}
                  onChange={(e) => setTestPrompt(e.target.value)}
                  style={{ resize: 'vertical' }}
                />
              </div>

              <button 
                className="btn btn-primary" 
                onClick={handleRunTest}
                disabled={testLoading}
                style={{ width: '100%', justifyContent: 'center' }}
              >
                {testLoading ? <RefreshCw size={16} className="animate-spin" /> : <Sparkles size={16} />}
                Send Completion Request
              </button>
            </div>

            <div>
              <label className="form-label">Gateway Inspection Output</label>
              {testResponse ? (
                <div style={{ background: 'var(--bg-primary)', border: '1px solid var(--card-border)', borderRadius: 'var(--radius-md)', padding: '16px' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                    <span className="badge badge-active">HTTP {testResponse.status}</span>
                    <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                      <span className="mono-code">{testResponse.latency} ms</span>
                      {testResponse.cacheHit && (
                        <span className="badge badge-cached">⚡ Semantic Cache Hit</span>
                      )}
                    </div>
                  </div>

                  <div style={{ fontSize: '13px', lineHeight: '1.6', color: 'var(--text-primary)', whiteSpace: 'pre-wrap', marginBottom: '16px' }}>
                    {testResponse.data?.choices?.[0]?.message?.content || JSON.stringify(testResponse.data, null, 2)}
                  </div>

                  {testResponse.data?.usage && (
                    <div style={{ fontSize: '11px', color: 'var(--text-muted)', borderTop: '1px solid var(--card-border)', paddingTop: '8px' }}>
                      Prompt Tokens: {testResponse.data.usage.prompt_tokens} | Completion Tokens: {testResponse.data.usage.completion_tokens} | Total: {testResponse.data.usage.total_tokens}
                    </div>
                  )}
                </div>
              ) : (
                <div style={{ background: 'var(--bg-primary)', border: '1px dashed var(--card-border)', borderRadius: 'var(--radius-md)', padding: '40px 20px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '14px' }}>
                  Click "Send Completion Request" to test the gateway live pipeline.
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Tab 3: Architecture & Routing */}
      {activeTab === 'architecture' && (
        <div className="table-card" style={{ padding: '24px' }}>
          <h2 className="table-title" style={{ marginBottom: '8px' }}>Provider Fallback &amp; Resilience Pipeline</h2>
          <p style={{ fontSize: '14px', color: 'var(--text-muted)', marginBottom: '24px' }}>
            Routewise wraps all upstream LLM providers in automatic circuit breakers. If a provider experiences timeouts or rate limits, the request seamlessly routes to the next healthy provider in sequence.
          </p>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: '16px' }}>
            <div style={{ background: 'var(--bg-tertiary)', padding: '20px', borderRadius: 'var(--radius-md)', border: '1px solid var(--card-border)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                <strong style={{ fontSize: '15px' }}>1. OpenRouter</strong>
                <span className="badge badge-active">Closed (Healthy)</span>
              </div>
              <p style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>Primary multi-model endpoint. Auto-trips after 5 consecutive failures.</p>
            </div>

            <div style={{ background: 'var(--bg-tertiary)', padding: '20px', borderRadius: 'var(--radius-md)', border: '1px solid var(--card-border)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                <strong style={{ fontSize: '15px' }}>2. Groq Cloud</strong>
                <span className="badge badge-active">Closed (Healthy)</span>
              </div>
              <p style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>High-speed LPU fallback. Engaged when OpenRouter degrades or exceeds quotas.</p>
            </div>

            <div style={{ background: 'var(--bg-tertiary)', padding: '20px', borderRadius: 'var(--radius-md)', border: '1px solid var(--card-border)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                <strong style={{ fontSize: '15px' }}>3. Mock Provider</strong>
                <span className="badge badge-active">Active Standby</span>
              </div>
              <p style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>Local zero-cost mock server used for load tests, benchmarks, and offline resilience.</p>
            </div>
          </div>
        </div>
      )}

      {/* Create Tenant Modal */}
      {showModal && (
        <div className="modal-overlay" onClick={() => setShowModal(false)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <h2 className="modal-title">Create New Tenant</h2>
            <p className="modal-desc">Configure tenant quota and generate an isolated API key.</p>

            {!createdApiKey ? (
              <form onSubmit={handleCreateTenant}>
                <div className="form-group">
                  <label className="form-label">Tenant / App Name</label>
                  <input 
                    type="text" 
                    className="form-input" 
                    placeholder="e.g. Mobile RAG App" 
                    value={modalName}
                    onChange={(e) => setModalName(e.target.value)}
                    required 
                  />
                </div>

                <div className="form-group">
                  <label className="form-label">Rate Limit (Requests per minute)</label>
                  <input 
                    type="number" 
                    className="form-input" 
                    value={modalRpm}
                    onChange={(e) => setModalRpm(e.target.value)}
                    min={1} 
                  />
                </div>

                <div className="form-group">
                  <label className="form-label">Daily Budget (USD)</label>
                  <input 
                    type="number" 
                    step="0.5"
                    className="form-input" 
                    value={modalBudget}
                    onChange={(e) => setModalBudget(e.target.value)}
                    min={0} 
                  />
                </div>

                <div className="modal-actions">
                  <button type="button" className="btn btn-secondary" onClick={() => setShowModal(false)}>
                    Cancel
                  </button>
                  <button type="submit" className="btn btn-primary">
                    Create Tenant &amp; Key
                  </button>
                </div>
              </form>
            ) : (
              <div>
                <div className="key-alert">
                  <p><strong>Copy your API Key now:</strong> For security, this secret key will not be displayed again.</p>
                  <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                    <input 
                      type="text" 
                      readOnly 
                      value={createdApiKey} 
                      className="form-input" 
                      style={{ fontFamily: 'var(--font-mono)', fontSize: '13px' }} 
                    />
                    <button 
                      className="btn btn-secondary btn-sm" 
                      onClick={() => copyToClipboard(createdApiKey)}
                      title="Copy API Key"
                    >
                      {copiedKey ? <Check size={16} color="#34d399" /> : <Copy size={16} />}
                    </button>
                  </div>
                </div>

                <div className="modal-actions">
                  <button className="btn btn-primary" onClick={() => setShowModal(false)}>
                    Done
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
