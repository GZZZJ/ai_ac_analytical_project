import React, { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './style.css'

const suggestions = [
  '腾讯近 7 天 CPS 为什么上涨？成本侧和单产侧各贡献多少？',
  '近 7 天巨量消耗为什么持续下滑？',
  '腾讯 RTA 策略 CPS 高但质量更好，还值得继续投吗？',
  '上周 3W2Q 提价有没有效果？'
]

async function api(path, method = 'GET', body) {
  const response = await fetch('/api' + path, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined })
  const data = await response.json()
  if (!response.ok) throw new Error(data.detail || '请求失败')
  return data
}

function fmt(value) {
  if (value == null) return '—'
  return typeof value === 'number' ? new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 4 }).format(value) : value
}

function MiniChart({ chart }) {
  const values = chart.values || []
  const max = Math.max(...values.map(v => Math.abs(v || 0)), .0001)
  if (chart.type === 'line' && values.length > 1) {
    const low = Math.min(...values), high = Math.max(...values)
    const span = high - low || 1
    const points = values.map((v, i) => `${12 + i * 276 / (values.length - 1)},${103 - (v - low) / span * 80}`).join(' ')
    return <div className="chart lineChart"><h4>{chart.title}</h4><svg viewBox="0 0 300 120" role="img" aria-label={chart.title}><line x1="12" y1="103" x2="288" y2="103" className="gridLine"/><line x1="12" y1="63" x2="288" y2="63" className="gridLine"/><line x1="12" y1="23" x2="288" y2="23" className="gridLine"/><polyline points={points} fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinejoin="round"/></svg><div className="chartAxis"><span>{chart.labels[0]}</span><strong>{fmt(values.at(-1))}</strong><span>{chart.labels.at(-1)}</span></div></div>
  }
  return <div className="chart"><h4>{chart.title}</h4><div className="chartRows">{chart.labels.map((label, i) => <div className="chartRow" key={i}><span>{label}</span><div className="barTrack"><div className={'bar ' + ((values[i] || 0) < 0 ? 'negative' : '')} style={{ width: `${Math.max(2, Math.abs(values[i] || 0) / max * 100)}%` }}/></div><strong>{fmt(values[i])}</strong></div>)}</div></div>
}

function Result({ item, onAsk, onModify }) {
  const result = item.result
  const [feedback, setFeedback] = useState('')
  const [reason, setReason] = useState('')
  const [note, setNote] = useState('')
  const [saved, setSaved] = useState(false)
  async function submitFeedback(type) {
    await api('/feedback', 'POST', { task_id: item.task_id, feedback_type: type, reason, text: note })
    setSaved(true)
  }
  if (!result) return <article className="result"><div className="assistantMark">析</div><div className="answerContent"><h3>分析暂停</h3><p>{item.answer}</p><Progress steps={item.progress} /></div></article>
  const originalQuery = result.handoffs?.task_spec?.original_query || ''
  if (/(投放效果|获客效果|整体表现)/.test(originalQuery) && result.intent === 'STRATEGY_REVIEW' && !/(3W2Q|策略|提价)/.test(originalQuery)) {
    return <article className="result"><div className="assistantMark">析</div><div className="answerContent"><h2>这条历史结果已失效</h2><p className="summary">系统曾把“{originalQuery}”误判为策略复盘。请重新发送原问题，系统会先确认指标和时间窗，再按实际渠道分析。</p></div></article>
  }
  return <article className="result">
    <div className="assistantMark" aria-hidden="true">析</div>
    <div className="answerContent">
      <div className="answerMeta">获客分析 <span>·</span> {result.reliability.level}可信度</div>
      <h2>{result.title}</h2>
      <p className="summary">{item.answer}</p>
      {result.handoffs?.task_spec && <div className="scope"><strong>分析范围</strong><span>{result.handoffs.task_spec.channels.join('、')} · {result.handoffs.task_spec.analysis_period.start} 至 {result.handoffs.task_spec.analysis_period.end}{result.handoffs.task_spec.baseline && <> · 对比 {result.handoffs.task_spec.baseline.start} 至 {result.handoffs.task_spec.baseline.end}</>} · 数据截至 {result.handoffs.task_spec.source_as_of}</span></div>}
      {result.analysis_steps?.length > 0 && <section className="section analysisChain"><h3>调查过程与证据</h3>{result.analysis_steps.map((step, i) => <div className="analysisStep" key={i}><strong>{step.title}</strong><p><b>方法</b> {step.method}</p><p><b>依据</b> {step.evidence.join('；')}</p><p><b>判断</b> {step.finding}</p><small>{step.boundary}</small></div>)}</section>}
      <section className="section keyFacts"><h3>关键依据</h3><ul>{result.facts.slice(0, result.handoffs?.task_spec?.channels?.length > 1 ? 4 : 3).map((fact, i) => <li key={i}>{fact}</li>)}</ul></section>
      {result.claims.length > 0 && <section className="section"><h3>原因判断</h3>{result.claims.map((claim, i) => <p key={i} className="claim"><span className={'tag ' + claim.status}>{claim.status}</span><span>{claim.text}</span><small>{claim.confidence}证据</small></p>)}</section>}
      <section className="section"><h3>建议动作</h3>{result.actions.map((a, i) => <div className="action" key={i}><strong>{a.action}</strong><p>{a.owner} · {a.when} · 观察 {a.observe}</p><p>预期：{a.expected}</p><p>回滚：{a.rollback}</p></div>)}</section>
      {result.investigation?.length > 0 && <section className="section"><h3>检查路径</h3>{result.investigation.map((step, i) => <p key={i} className="fact">{i === 0 ? `先比较规模、成本和质量；${step.reason}` : `${step.checked?.replace('COST_ANOMALY', '成本与单产').replace('SCALE_ANOMALY', '拿量链路').replace('QUALITY_CHECK', '质量分组')}：${step.observation}。${step.reason}`}</p>)}</section>}
      <details className="analysisDetails"><summary>查看完整分析与图表</summary><div className="detailBody"><Progress steps={item.progress}/>{result.facts.slice(result.handoffs?.task_spec?.channels?.length > 1 ? 4 : 3).map((fact, i) => <p key={i} className="fact">{fact}</p>)}<div className="charts">{result.charts.map((chart, i) => <MiniChart chart={chart} key={i}/>)}</div><h3>可信度与限制</h3><div className="dimensions">{Object.entries(result.reliability.dimensions).map(([k, v]) => <span key={k}>{k}：{v}</span>)}</div>{result.reliability.limitations.map((text, i) => <p className="limit" key={i}>{text}</p>)}</div></details>
      <details className="analysisDetails"><summary>数据与资料来源</summary><div className="sourceList">{result.evidence.map((s, i) => <div key={i}>{s.table || s.knowledge} · {s.range || s.rule} {s.rows != null ? `· ${s.rows} 行` : ''}{s.filters && Object.keys(s.filters).length ? ` · ${JSON.stringify(s.filters)}` : ''}</div>)}</div></details>
      <div className="answerTools"><a href={'/api/tasks/' + item.task_id + '/export.md'} download>↓ 导出 Markdown</a>{onModify && <button onClick={onModify}>✎ 修改条件</button>}{saved ? <span>反馈已记录</span> : <><button onClick={() => submitFeedback('positive')}>有帮助</button><button onClick={() => setFeedback('negative')}>需要改进</button></>}</div>
      {feedback && !saved && <div className="feedbackForm"><select value={reason} onChange={e => setReason(e.target.value)}><option value="">选择原因</option>{['没理解我的问题','数据 / 数值不对','分析不够深入','结论太强 / 不可信','建议不实用','图表 / 表达不好','其他'].map(x => <option key={x}>{x}</option>)}</select><input value={note} onChange={e => setNote(e.target.value)} placeholder="补充说明（可选）"/><button onClick={() => submitFeedback('negative')}>提交反馈</button></div>}
      <div className="followups"><strong>可以继续问</strong>{result.next_questions.map((q, i) => <button key={i} onClick={() => onAsk(q)}>{q} <span>↗</span></button>)}</div>
    </div>
  </article>
}

function Progress({ steps = [], onCancel, active = false }) {
  const stages = [
    ['确认口径与数据', '确认范围'],
    ['计算指标与对照', '核对成本和规模数据'],
    ['核对证据边界', '比较变化并检查主要判断'],
    ['形成结论与动作', '形成结论与建议'],
    ['独立复核与报告一致性', '复核报告']
  ]
  const staged = !steps.length || steps.some(step => stages.some(([key]) => key === step.name))
  const complete = steps.filter(step => step.status === 'completed').length
  const rows = staged ? stages.map(([key, label], i) => ({name: label, status: steps.find(step => step.name === key)?.status || (active && i === complete ? 'running' : 'pending')})) : steps
  return <ol className="progress">{rows.map((step, i) => <li key={i} className={step.status}><span className="progressName">{step.name}</span><span className="progressStatus">{step.status === 'completed' ? '已完成' : step.status === 'running' ? '进行中' : step.status === 'waiting_user' ? '待确认' : step.status === 'blocked' || step.status === 'failed' ? '已暂停' : step.status === 'cancelled' ? '已取消' : '待开始'}</span>{onCancel && step.task_id && ['pending', 'running', 'waiting_user'].includes(step.status) && <button className="cancelStep" onClick={() => onCancel(step.task_id)}>取消此项</button>}</li>)}</ol>
}

function Batch({ item, onAsk, onCancel, onModify }) {
  return <div className="batch"><h2>分项分析 · {item.status === 'completed' ? '全部完成' : '部分完成'}</h2><Progress steps={item.progress} onCancel={onCancel}/>{item.items.map((part, i) => <section key={i}><h3>{item.plan.steps[i].question}</h3>{part.type === 'result' || part.type === 'blocked' ? <Result item={part} onAsk={onAsk} onModify={part.type === 'result' ? () => onModify(part.task_id) : null}/> : part.type === 'clarification' ? <Clarification data={part.clarification} onSubmit={answers => onAsk('', {...answers, __task_id: part.task_id})} onCancel={() => onCancel(part.task_id)}/> : <p>{part.answer}</p>}</section>)}{item.previousVersions?.length > 0 && <details><summary>查看修改前版本</summary>{item.previousVersions.map((part, i) => <Result key={i} item={part} onAsk={onAsk}/>)}</details>}</div>
}

function Clarification({ data, onSubmit, onCancel }) {
  const [answers, setAnswers] = useState(() => Object.fromEntries(data.questions.filter(q => q.default).map(q => [q.key, q.default])))
  const [customDays, setCustomDays] = useState(false)
  const days = Number(String(answers.days || '').match(/\d+/)?.[0])
  const asOf = data.source_as_of
  const shift = (date, count) => new Date(Date.parse(date + 'T00:00:00Z') + count * 86400000).toISOString().slice(0, 10)
  const range = asOf && days >= 1 && days <= 30 ? {start: shift(asOf, 1 - days), end: asOf, baselineStart: shift(asOf, 1 - 2 * days), baselineEnd: shift(asOf, -days)} : null
  return <article className="clarification">
    <div className="clarificationKicker">先确认范围</div>
    <h3>确认一下分析条件</h3>
    <p>选好后会按实际日期取数；你也可以在输入框里直接补充条件。</p>
    <div className="clarificationPanel">
      <strong className="panelTitle">分析范围</strong>
      {data.questions.map(q => <div className="question" key={q.key}><strong>{q.label}</strong><div className="choiceList">{q.options.map(option => <button key={option} className={answers[q.key] === option ? 'selected' : ''} aria-pressed={answers[q.key] === option} onClick={() => {setAnswers({...answers, [q.key]: option}); if (q.key === 'days') setCustomDays(false)}}>{option}</button>)}{q.key === 'days' && <button className={customDays ? 'selected' : ''} aria-pressed={customDays} onClick={() => {setCustomDays(true); setAnswers({...answers, days: ''})}}>自定义</button>}</div>{q.key === 'days' && customDays && <label className="customDays">天数（1–30） <input type="number" min="1" max="30" value={days || ''} onChange={e => setAnswers({...answers, days: e.target.value ? `近 ${e.target.value} 天` : ''})}/></label>}</div>)}
      {range && <div className="rangePreview"><div>目标期：{range.start} 至 {range.end}</div><div>对比期：{range.baselineStart} 至 {range.baselineEnd}（前一等长周期）</div></div>}
      <p className="maturityNote">数据截至 {asOf || '模拟数据基准日'}；转化为 {data.maturity}，近 7 天不代表 T7 均已成熟。</p>
      <div className="clarificationActions"><button className="primary" disabled={data.questions.some(q => !answers[q.key]) || (customDays && !(days >= 1 && days <= 30))} onClick={() => onSubmit(answers)}>确认并分析</button>{onCancel && <button className="textButton" onClick={onCancel}>取消</button>}</div>
    </div>
  </article>
}

function App() {
  const [sessions, setSessions] = useState([])
  const [dataset, setDataset] = useState(null)
  const [sessionId, setSessionId] = useState(null)
  const [items, setItems] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [jobId, setJobId] = useState(null)
  const [jobProgress, setJobProgress] = useState([])
  const [activeQuery, setActiveQuery] = useState('')
  const [stopping, setStopping] = useState(false)
  const [error, setError] = useState('')

  async function refreshSessions() { setSessions(await api('/sessions')) }
  async function createSession() { if (busy) return; const x = await api('/sessions', 'POST'); setSessionId(x.session_id); setItems([]); refreshSessions() }
  async function renameSession(id, oldTitle) { const title = window.prompt('会话标题', oldTitle || '分析会话'); if (title === null) return; try { await api('/sessions/' + id, 'PATCH', { title }); refreshSessions() } catch (err) { setError(String(err.message || err)) } }
  async function deleteSession(id) { if (busy || !window.confirm('删除这个会话及其分析记录？')) return; try { await api('/sessions/' + id, 'DELETE'); if (sessionId === id) { setSessionId(null); setItems([]) }; refreshSessions() } catch (err) { setError(String(err.message || err)) } }
  async function openSession(id) { if (busy) return; const x = await api('/sessions/' + id); setSessionId(id); const tasks = Object.fromEntries((x.tasks || []).map(t => [t.id, t])); const restored = x.messages.map(m => { const task = tasks[m.task_id]; return m.role === 'assistant' && task?.result && m.content === task.result.answer ? { type: 'result', task_id: task.id, answer: m.content, result: task.result, progress: task.state.progress } : { type: m.role, text: m.content } }); for (const pending of x.clarifications || []) restored.push({ type: 'clarification', task_id: pending.task_id, clarification: pending.clarification }); setItems(restored) }
  useEffect(() => { api('/dataset').then(setDataset).catch(setError); api('/sessions').then(data => { setSessions(data); if (data.length) openSession(data[0].id) }).catch(setError); }, [])
  async function send(text = input, answers = null, targetTaskId = null) {
    if ((!text.trim() && !answers) || busy) return
    setError(''); setBusy(true); setActiveQuery(text || '按已确认条件继续分析')
    try {
      let id = sessionId
      if (!id) { const s = await api('/sessions', 'POST'); id = s.session_id; setSessionId(id); refreshSessions() }
      if (!answers) setItems(prev => [...prev, { type: 'user', text }])
      setInput('')
      const started = await api('/sessions/' + id + '/jobs', 'POST', { query: answers ? '' : text, answers, target_task_id: targetTaskId })
      setJobId(started.job_id)
      for (;;) {
        const job = await api('/jobs/' + started.job_id)
        setJobProgress(job.progress || [])
        if (job.status === 'completed') {
          setItems(prev => {
            if (!answers && !targetTaskId) return [...prev, job.response]
            let replaced = false
            const updated = prev.map(item => {
              if (answers && item.type === 'clarification' && item.task_id === answers.__task_id) { replaced = true; return job.response }
              if (item.type === 'batch') {
                const index = item.items.findIndex(part => part.task_id === (answers?.__task_id || targetTaskId))
                if (index >= 0) {
                  replaced = true
                  const parts = [...item.items]; parts[index] = job.response
                  const steps = item.plan.steps.map((step, i) => i === index ? {...step, task_id: job.response.task_id, status: job.response.type === 'result' ? 'COMPLETED' : job.response.type === 'clarification' ? 'WAITING_USER' : 'FAILED'} : step)
                  return {...item, items: parts, previousVersions: targetTaskId ? [...(item.previousVersions || []), item.items[index]] : item.previousVersions, plan: {...item.plan, steps}, status: parts.every(part => part.type === 'result') ? 'completed' : 'partial', progress: steps.map(step => ({name: step.question, task_id: step.task_id, status: step.status.toLowerCase()}))}
                }
              }
              return item
            })
            return replaced ? updated : [...updated, job.response]
          }); break
        }
        if (job.status === 'stopped') { setItems(prev => [...prev, { type: 'assistant', text: '本次分析已停止。' }]); break }
        if (job.status === 'failed') throw new Error(job.error || '分析失败')
        await new Promise(resolve => setTimeout(resolve, 450))
      }
      refreshSessions()
    } catch (err) { setError(String(err.message || err)) } finally { setBusy(false); setJobId(null); setJobProgress([]); setActiveQuery(''); setStopping(false) }
  }
  async function stop() { if (!jobId) return; setStopping(true); try { await api('/jobs/' + jobId + '/stop', 'POST') } catch (err) { setError(String(err.message || err)) } }
  async function modifyTask(taskId) { const query = window.prompt('输入此项的新范围或条件，例如“换成抖音看近 14 天 CPS”'); if (query?.trim()) await send(query.trim(), null, taskId) }
  async function cancelSubtask(taskId) { try { const response = await api('/tasks/' + taskId + '/cancel', 'POST'); if (response.status === 'cancelled') setItems(prev => prev.map(item => item.type === 'batch' ? {...item, items: item.items.map(part => part.task_id === taskId ? {type: 'cancelled', task_id: taskId, answer: '此分项已取消'} : part), progress: item.progress.map(step => step.task_id === taskId ? {...step, status: 'cancelled'} : step)} : item.type === 'clarification' && item.task_id === taskId ? {type: 'assistant', text: '此分项已取消'} : item)) } catch (err) { setError(String(err.message || err)) } }
  return <div className="app"><aside><div className="brand"><span className="brandIcon">A<span>·</span></span><span>获客智能分析<small>ACQUISITION INTELLIGENCE</small></span></div><button className="new" onClick={createSession}>＋ <span>新建分析</span></button><div className="asideTitle">WORKSPACE / 会话</div>{sessions.map((s, i) => <div className="sessionRow" key={s.id}><button className={'session ' + (sessionId === s.id ? 'active' : '')} onClick={() => openSession(s.id)}><span className="sessionIndex">{String(sessions.length - i).padStart(2, '0')}</span> {s.title || '分析会话'}<small>{s.created_at.slice(0, 16).replace('T', ' ')}</small></button><div className="sessionActions"><button aria-label="重命名" title="重命名" onClick={() => renameSession(s.id, s.title)}>✎</button><button aria-label="删除" title="删除" onClick={() => deleteSession(s.id)}>×</button></div></div>)}<div className="asideBottom"><span>DATASET 01</span><br/>模拟数据 · 截至 {dataset?.as_of || '2026-09-22'}</div></aside>
    <main><header><span><b>业务分析工作台</b><small>/ CONVERSATIONAL ANALYST</small></span><span className="headerRight">T0–T7 离线终值 <i/> LOCAL MVP</span></header><div className="conversation">{items.length === 0 && <div className="welcome"><div className="welcomeIndex">01 / ASK THE DATA</div><div className="orbit">↗</div><h1>把投放问题，<br/><em>变成可核查的结论。</em></h1><p>描述你观察到的变化。系统会核对口径和数据，拆解可能原因，并给出下一步动作。</p><div className="suggestions"><div className="suggestionLabel">从典型问题开始 <span>SELECT A QUESTION ↓</span></div>{suggestions.map((q, i) => <button onClick={() => send(q)} key={q}><span className="suggestionNo">0{i + 1}</span><span className="suggestionText">{q}</span><span className="arrow">↗</span></button>)}</div></div>}{items.map((item, i) => <div className={'message message-' + item.type} key={i}>{item.type === 'user' ? <div className="userBubble">{item.text}</div> : item.type === 'assistant' ? <div className="plainAnswer">{item.text}</div> : item.type === 'clarification' ? <Clarification data={item.clarification} onSubmit={answers => send('', {...answers, __task_id: item.task_id})} onCancel={() => cancelSubtask(item.task_id)}/> : item.type === 'side_query' ? <div className="plainAnswer">{item.answer}</div> : item.type === 'batch' ? <Batch item={item} onAsk={send} onCancel={cancelSubtask} onModify={modifyTask}/> : item.type === 'result' || item.type === 'blocked' ? <Result item={item} onAsk={send} onModify={item.type === 'result' ? () => modifyTask(item.task_id) : null}/> : null}</div>)}{busy && <div className="loading"><div className="loadingTop"><span><span className="spinner"/> {stopping ? '正在停止当前步骤…' : '正在分析'}</span><button onClick={stop} disabled={!jobId || stopping}>{stopping ? '停止中' : '停止分析'}</button></div>{jobProgress.find(step => step.scope)?.scope ? <div className="confirmedScope"><strong>已确认范围</strong><p>{jobProgress.find(step => step.scope).scope.channels.join('、')} · {jobProgress.find(step => step.scope).scope.analysis_period.start} 至 {jobProgress.find(step => step.scope).scope.analysis_period.end}</p><small>对比 {jobProgress.find(step => step.scope).scope.baseline?.start || '—'} 至 {jobProgress.find(step => step.scope).scope.baseline?.end || '—'} · 数据截至 {jobProgress.find(step => step.scope).scope.source_as_of}</small></div> : <div className="confirmedScope"><strong>正在确认范围</strong><p>{activeQuery}</p><small>模拟数据截至 {dataset?.as_of || '2026-09-22'}；实际分析日期以报告为准</small></div>}<Progress steps={jobProgress} onCancel={cancelSubtask} active/></div>}</div><div className="composer">{error && <div className="error">{error}</div>}<div className="inputWrap"><textarea value={input} onChange={e => setInput(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }} placeholder="输入你想分析的获客问题…" rows="2"/><button onClick={() => send()} disabled={busy || !input.trim()}>发送 ↗</button></div><div className="hint">ENTER 发送　·　SHIFT + ENTER 换行　·　模拟数据仅供分析验证</div></div></main></div>
}

createRoot(document.getElementById('root')).render(<App />)
