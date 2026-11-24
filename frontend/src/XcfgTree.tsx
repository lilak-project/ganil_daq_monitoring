import { createContext, useContext, useEffect, useMemo, useState } from "react"
import { Command, NoticeLine } from "@neon"

import { inRange, parseRange } from "./xcfgValue"

type Row = {
  name: string
  index: string
  object: boolean
  value: string | null
  unit?: string | null
  range?: string | null
  /** Where the value was found, when not on this object itself: a default (`*`). */
  from: string | null
  /** Backtrack level: 0 = the object's own; more = inherited (cconfiged's BL column, blue). */
  bl: number
  /** An own value a default also sets (dark red in cconfiged). */
  override: boolean
  include?: string | null
  /** cconfiged's bold name: an object whose own subtree holds a value. */
  bold: boolean
  /** ConfigNode::hasValidValue: false paints the row red, as cconfiged does. */
  valid: boolean
  comment: string
  /** A value of this very object, written in place on Save. */
  editable: boolean
  type: string | null
}
type Key = [string, string]
type Edit = { keys: Key[]; value: string; original: string; range: string | null }
type Hit = { keys: Key[]; label: string; value: string | null }

const label = (r: { name: string; index: string }) => (r.index ? `${r.name}[${r.index}]` : r.name)
const pathOf = (keys: Key[]) => keys.map(([n, i]) => (i ? `${n}[${i}]` : n)).join(".")

type Ctx = {
  step: string
  name: string
  control: boolean
  edits: Map<string, Edit>
  setEdit: (edit: Edit | null, path: string) => void
  reveal: string | null
  version: number
  onSha: (sha: string) => void
}
const TreeContext = createContext<Ctx | null>(null)

/**
 * An ECC configuration as cconfiged shows it (ConfigWidgets' ConfigModel): under
 * every object, its own attributes and its defaults' (`*`), default objects
 * included, with cconfiged's columns -- Name, Value, Unit, Range, BL -- and its
 * marks: inherited (BL > 0) in the accent where cconfiged has blue, an own value
 * overriding a default in pink where it has dark red, bold names for objects
 * holding values of their own, a red row for a value outside its range, the
 * file's comments on the name. Resolved on the server exactly as CompoundConfig
 * does (xcfg.py, checked row for row against the library on every grgroup file).
 *
 * In Control, an object's own values can be edited (double-click, Enter) and
 * saved into the file in place -- backed up first, refused out of range. An
 * inherited value is changed by overriding it (step 2).
 */
export function XcfgTree({ step, name, control }: { step: string; name: string; control: boolean }) {
  const [edits, setEdits] = useState<Map<string, Edit>>(new Map())
  const [sha, setSha] = useState<string | null>(null)
  const [version, setVersion] = useState(0)
  const [said, setSaid] = useState<{ ok: boolean; text: string } | null>(null)
  const [saving, setSaving] = useState(false)
  const [query, setQuery] = useState("")
  const [hits, setHits] = useState<{ hits: Hit[]; truncated: boolean } | null>(null)
  const [reveal, setReveal] = useState<string | null>(null)

  useEffect(() => {
    setEdits(new Map())
    setHits(null)
    setReveal(null)
    setSaid(null)
  }, [step, name])

  const ctx = useMemo<Ctx>(
    () => ({
      step,
      name,
      control,
      edits,
      setEdit: (edit, path) =>
        setEdits((now) => {
          const next = new Map(now)
          if (edit && edit.value !== edit.original) next.set(path, edit)
          else next.delete(path)
          return next
        }),
      reveal,
      version,
      onSha: setSha,
    }),
    [step, name, control, edits, reveal, version],
  )

  const bad = [...edits.values()].filter((e) => !inRange(e.value, parseRange(e.range)))
  const save = async () => {
    setSaving(true)
    try {
      const answer = await fetch("api/rcc/ecc-file", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ step, name, sha, edits: [...edits.values()].map(({ keys, value }) => ({ keys, value })) }),
      })
      const data = await answer.json().catch(() => ({}))
      if (!answer.ok) setSaid({ ok: false, text: data.detail ?? answer.statusText })
      else {
        setSaid({ ok: true, text: `Saved ${data.changed} value(s) into ${data.path} · previous file kept as ${data.backup.split("/").pop()}` })
        setEdits(new Map())
        setVersion((v) => v + 1)
      }
    } catch (err) {
      setSaid({ ok: false, text: String(err) })
    } finally {
      setSaving(false)
    }
  }
  const find = async () => {
    if (!query.trim()) return setHits(null)
    const answer = await fetch(`api/rcc/ecc-find?step=${encodeURIComponent(step)}&name=${encodeURIComponent(name)}&q=${encodeURIComponent(query)}`)
    const data = await answer.json().catch(() => ({ hits: [] }))
    setHits(answer.ok ? data : { hits: [], truncated: false })
  }

  return (
    <TreeContext.Provider value={ctx}>
      <div className="gn-stack gn-tight">
        <div className="gn-tags">
          <input
            className="neon-input gn-find"
            placeholder="Find a name or value (Enter)"
            value={query}
            onChange={(e) => setQuery(e.currentTarget.value)}
            onKeyDown={(e) => e.key === "Enter" && void find()}
          />
          <Command small onRun={() => void find()}>
            Find
          </Command>
          {control ? (
            <>
              <span className="faint">{edits.size ? `${edits.size} unsaved` : "double-click a value of its own to edit"}</span>
              <Command small disabled={!edits.size || saving} onRun={() => setEdits(new Map())}>
                Discard
              </Command>
              <Command small primary disabled={!edits.size || saving || bad.length > 0} onRun={() => void save()}>
                {saving ? "…" : "Save"}
              </Command>
            </>
          ) : null}
        </div>
        {said || bad.length ? (
          <NoticeLine tone={bad.length || (said && !said.ok) ? "warn" : null}>
            {bad.length ? `${bad.length} value(s) outside their range: ${bad.map((e) => pathOf(e.keys)).join(", ")}` : said!.text}
          </NoticeLine>
        ) : null}
        {hits ? (
          <div className="gn-hits">
            {hits.hits.length === 0 ? <span className="faint">nothing found</span> : null}
            {hits.hits.map((hit) => (
              <button key={hit.label} type="button" className="gn-hit" onClick={() => setReveal(hit.label)}>
                {hit.label}
                {hit.value !== null ? <span className="faint"> = {hit.value}</span> : null}
              </button>
            ))}
            {hits.truncated ? <span className="faint">first 200 shown</span> : null}
          </div>
        ) : null}
        <div className="gn-tree">
          <div className="gn-tree-row gn-tree-head">
            <span className="gn-tree-name">Name</span>
            <span className="gn-tree-val">Value</span>
            <span className="gn-tree-unit">Unit</span>
            <span className="gn-tree-range">Range</span>
            <span className="gn-tree-bl">BL</span>
          </div>
          <Level keys={[]} depth={0} />
        </div>
      </div>
    </TreeContext.Provider>
  )
}

function Level({ keys, depth }: { keys: Key[]; depth: number }) {
  const ctx = useContext(TreeContext)!
  const [rows, setRows] = useState<Row[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState<Set<string>>(new Set())
  const [editing, setEditing] = useState<string | null>(null)
  const here = pathOf(keys)

  useEffect(() => {
    setRows(null)
    const url = `api/rcc/ecc-tree?step=${encodeURIComponent(ctx.step)}&name=${encodeURIComponent(ctx.name)}&path=${encodeURIComponent(JSON.stringify(keys))}`
    fetch(url)
      .then(async (answer) => {
        const data = await answer.json()
        if (!answer.ok) throw new Error(data.detail ?? answer.statusText)
        setRows(data.rows)
        if (keys.length === 0) ctx.onSha(data.sha)
      })
      .catch((err) => setError(String(err instanceof Error ? err.message : err)))
  }, [ctx.step, ctx.name, here, ctx.version])

  // Find: open the way down to the hit.
  useEffect(() => {
    if (!ctx.reveal || !rows) return
    const child = rows.find((r) => {
      const p = here ? `${here}.${label(r)}` : label(r)
      return r.object && (ctx.reveal === p || ctx.reveal!.startsWith(p + "."))
    })
    if (child) setOpen((now) => new Set(now).add(label(child)))
  }, [ctx.reveal, rows])

  if (error) return <div className="gn-tree-row tone-text-warn">{error}</div>
  if (!rows) return <div className="gn-tree-row faint">…</div>
  const toggle = (k: string) =>
    setOpen((now) => {
      const next = new Set(now)
      if (next.has(k)) next.delete(k)
      else next.add(k)
      return next
    })

  return (
    <>
      {rows.map((row) => {
        const k = label(row)
        const rowKeys: Key[] = [...keys, [row.name, row.index]]
        const path = pathOf(rowKeys)
        const isOpen = open.has(k)
        const edit = ctx.edits.get(path)
        const value = edit ? edit.value : row.value
        const valid = edit ? inRange(edit.value, parseRange(row.range)) : row.valid
        // cconfiged's ConfigTreeColumn colours: own black, override dark red, inherited blue.
        const tone = row.bl > 0 ? "gn-tree-inh" : row.override ? "gn-tree-ovr" : ""
        const title = [row.comment, row.from ? `from ${row.from}` : "", row.range ? `range ${row.range}` : "", row.include ? `include ${row.include}` : ""]
          .filter(Boolean)
          .join("\n")
        const canEdit = ctx.control && row.editable
        const enumValues = (() => {
          const r = parseRange(row.range)
          return r && r.kind === "enum" ? r.raw : null
        })()
        return (
          <div key={k}>
            <div
              className={`gn-tree-row ${tone}${row.object ? " gn-tree-obj" : ""}${valid ? "" : " gn-tree-invalid"}${edit ? " gn-tree-dirty" : ""}${ctx.reveal === path ? " gn-tree-hit" : ""}`}
              title={title || undefined}
              ref={(el) => {
                if (el && ctx.reveal === path) el.scrollIntoView({ block: "center" })
              }}
              onClick={row.object ? () => toggle(k) : undefined}
              onDoubleClick={canEdit ? () => setEditing(k) : undefined}
            >
              <span className={`gn-tree-name${row.bold ? " gn-tree-bold" : ""}`} style={{ paddingLeft: depth * 14 }}>
                <span className="faint gn-caret">{row.object ? (isOpen ? "▾" : "▸") : ""}</span>
                {k}
              </span>
              <span className="gn-tree-val">
                {row.object ? (
                  ""
                ) : editing === k ? (
                  enumValues || row.type === "bool" ? (
                    <select
                      className="neon-input gn-tree-edit"
                      autoFocus
                      value={value ?? ""}
                      onChange={(e) => {
                        ctx.setEdit({ keys: rowKeys, value: e.currentTarget.value, original: row.value ?? "", range: row.range ?? null }, path)
                        setEditing(null)
                      }}
                      onBlur={() => setEditing(null)}
                    >
                      {(enumValues ?? ["true", "false"]).map((v) => (
                        <option key={v} value={v}>
                          {v}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <input
                      className="neon-input gn-tree-edit"
                      autoFocus
                      defaultValue={value ?? ""}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          ctx.setEdit({ keys: rowKeys, value: e.currentTarget.value.trim(), original: row.value ?? "", range: row.range ?? null }, path)
                          setEditing(null)
                        }
                        if (e.key === "Escape") setEditing(null)
                      }}
                      onBlur={(e) => {
                        ctx.setEdit({ keys: rowKeys, value: e.currentTarget.value.trim(), original: row.value ?? "", range: row.range ?? null }, path)
                        setEditing(null)
                      }}
                    />
                  )
                ) : (
                  value
                )}
              </span>
              <span className="gn-tree-unit">{row.unit ?? ""}</span>
              <span className="gn-tree-range">{row.range ?? ""}</span>
              <span className="gn-tree-bl">{row.bl}</span>
            </div>
            {row.object && isOpen ? <Level keys={rowKeys} depth={depth + 1} /> : null}
          </div>
        )
      })}
    </>
  )
}
