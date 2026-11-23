import { useEffect, useState } from "react"
import { Command, Events, NoticeLine, Panel } from "@neon"

type Values = { acq_root: string; rcc_host: string; rcc_port: number; rcc_poll: number }
type Loaded = {
  values: Values
  defaults: Values
  experiments: { name: string; acq_root: string }[]
  experiments_dir: string
  /** What each experiment remembers, by acquisition folder. */
  saved: Record<string, Partial<Values>>
}
type AuditEntry = { at: number; who: string; command: string; ok: boolean; reply: string }

/**
 * What this service reads and whom it asks. Saved on the server and applied at
 * once; the manifest's environment only gives the first values.
 */
export function Settings() {
  const [loaded, setLoaded] = useState<Loaded | null>(null)
  const [draft, setDraft] = useState<Values | null>(null)
  const [said, setSaid] = useState<{ ok: boolean; text: string } | null>(null)
  const [saving, setSaving] = useState(false)
  const [audit, setAudit] = useState<AuditEntry[]>([])

  const load = async () => {
    const [s, a] = await Promise.all([fetch("api/settings").then((r) => r.json()), fetch("api/audit?limit=30").then((r) => r.json())])
    setLoaded(s)
    setDraft(s.values)
    setAudit(a.entries ?? [])
  }
  useEffect(() => {
    void load().catch((err) => setSaid({ ok: false, text: String(err) }))
  }, [])

  if (!loaded || !draft) return <Panel title="Settings">…</Panel>
  const dirty = (Object.keys(draft) as (keyof Values)[]).some((k) => String(draft[k]) !== String(loaded.values[k]))
  const set = (key: keyof Values, value: string) => setDraft({ ...draft, [key]: value })
  /** Picking an experiment brings back what it remembers; one never saved keeps the values on screen. */
  const pick = (acqRoot: string) => setDraft({ ...draft, ...(loaded.saved[acqRoot] ?? {}), acq_root: acqRoot })

  const save = async () => {
    setSaving(true)
    try {
      const answer = await fetch("api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(draft) })
      const data = await answer.json().catch(() => ({}))
      if (!answer.ok) setSaid({ ok: false, text: data.detail ?? answer.statusText })
      else {
        setSaid({ ok: true, text: data.changed.length ? `Saved and applied: ${data.changed.join(", ")}` : "Nothing changed" })
        await load()
      }
    } catch (err) {
      setSaid({ ok: false, text: String(err) })
    } finally {
      setSaving(false)
    }
  }

  const field = (key: keyof Values, label: string, hint: string, wide = false) => (
    <label className="gn-field">
      <span className="gn-field-label">{label}</span>
      <input className={`neon-input${wide ? " gn-wide" : " num"}`} value={String(draft[key])} onChange={(e) => set(key, e.currentTarget.value)} />
      <span className="faint">
        {hint} · first value {String(loaded.defaults[key])}
      </span>
    </label>
  )

  return (
    <div className="gn-stack">
      <Panel
        title="Experiment"
        actions={
          <span className="gn-tags">
            <Command small disabled={!dirty || saving} onRun={() => setDraft(loaded.values)}>
              Undo
            </Command>
            <Command small primary disabled={!dirty || saving} onRun={() => void save()}>
              {saving ? "…" : "Save"}
            </Command>
          </span>
        }
      >
        <div className="gn-stack">
          <NoticeLine tone={said && !said.ok ? "warn" : null}>{said?.text ?? "Each experiment keeps its own settings (●). Picking one brings them back; Save applies at once, without a restart."}</NoticeLine>
          {/* The experiments beside this one: every folder with acquisition/log in it. */}
          <div className="gn-tags">
            {loaded.experiments.length === 0 ? (
              <span className="faint">No experiments found in {loaded.experiments_dir}</span>
            ) : (
              loaded.experiments.map((exp) => (
                <Command key={exp.name} small on={draft.acq_root === exp.acq_root} onRun={() => pick(exp.acq_root)}>
                  {exp.name}
                  {loaded.saved[exp.acq_root] ? <span className="faint" title="has saved settings"> ●</span> : null}
                </Command>
              ))
            )}
          </div>
          {field("acq_root", "Acquisition folder", `logs in log/, data in run/ · experiments listed from ${loaded.experiments_dir}`, true)}
        </div>
      </Panel>

      <Panel title="Run control">
        <div className="gn-row">
          {field("rcc_host", "Host", "where the RCC answers SOAP")}
          {field("rcc_port", "Port", "6911 on GANIL setups")}
          {field("rcc_poll", "Ask every (s)", "1 to 60")}
        </div>
      </Panel>

      <Panel title="Commands and changes · newest first">
        <Events
          items={audit.map((entry) => ({
            at: new Date(entry.at * 1000).toLocaleString(),
            text: `${entry.who} · ${entry.command} · ${entry.reply}`,
            tone: entry.ok ? undefined : "warn",
          }))}
        />
      </Panel>
    </div>
  )
}
