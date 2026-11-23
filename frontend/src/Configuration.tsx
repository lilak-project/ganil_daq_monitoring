import { Fragment, useEffect, useState } from "react"
import { Table } from "@mantine/core"
import { Command, Facts, NoticeLine, Panel } from "@neon"

type Parameter = { name: string; value?: string; type?: string; permission?: string; dynamic?: string }
type Equipment = {
  kind: string
  name: string
  parent: string
  attrs: Record<string, string>
  parameters: Parameter[]
  ecc_configs?: Record<string, string>
}
type Config = {
  path: string
  container: string
  text: string
  equipment: Equipment[]
  links: Record<string, string>[]
  parse_error?: string
}

/** The attributes worth a column; the rest show when the row is opened. */
const MAIN = ["hostname", "port", "executable_file", "log_level"]

/**
 * The configuration the run control has loaded (rccGetExperimentCfgFile),
 * read out of the container and laid out: which ECC configurations are in
 * use, every equipment with its parameters, and the links between them. The
 * raw XML is a press away.
 */
export function Configuration({ path }: { path: string | null }) {
  const [config, setConfig] = useState<Config | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState<Set<string>>(new Set())
  const [raw, setRaw] = useState(false)

  const load = async () => {
    setError(null)
    try {
      const answer = await fetch("api/rcc/config")
      const data = await answer.json()
      if (!answer.ok) throw new Error(data.detail ?? answer.statusText)
      setConfig(data)
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err))
    }
  }
  // Read again whenever the run control loads another file.
  useEffect(() => {
    void load()
  }, [path])

  const toggle = (name: string) =>
    setOpen((now) => {
      const next = new Set(now)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  const ecc = config?.equipment.find((e) => e.ecc_configs)

  return (
    <div className="gn-stack">
      <Panel
        title="Loaded configuration"
        actions={
          <span className="gn-tags">
            <Command small onRun={() => void load()}>
              Read again
            </Command>
            <Command small on={raw} onRun={() => setRaw(!raw)}>
              Raw XML
            </Command>
          </span>
        }
      >
        <NoticeLine tone={error || config?.parse_error ? "warn" : null}>
          {error ?? config?.parse_error ?? (config ? `${config.equipment.length} equipment · ${config.links.length} links · read from ${config.container}` : "reading…")}
        </NoticeLine>
        <div className="gn-path">{config?.path ?? path ?? "—"}</div>
        {ecc?.ecc_configs ? (
          <Facts items={Object.entries(ecc.ecc_configs).map(([step, name]) => [`ECC ${step}`, <b key={step}>{name}</b>])} />
        ) : null}
      </Panel>

      {raw && config ? (
        <Panel title="Raw XML">
          <pre className="gn-raw">{config.text}</pre>
        </Panel>
      ) : null}

      {config ? (
        <Panel title={`Equipment · ${config.equipment.length}`}>
          <Table highlightOnHover verticalSpacing={4} className="gn-filetable">
            <Table.Thead>
              <Table.Tr>
                <Table.Th w={170}>Name</Table.Th>
                <Table.Th w={90}>Kind</Table.Th>
                <Table.Th w={90}>In</Table.Th>
                {MAIN.map((k) => (
                  <Table.Th key={k}>{k.replace("_", " ")}</Table.Th>
                ))}
                <Table.Th w={90} className="gn-right">Parameters</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {config.equipment.map((item) => {
                const isOpen = open.has(item.name)
                const rest = Object.entries(item.attrs).filter(([k]) => !MAIN.includes(k) && k !== "configid")
                return (
                  <Fragment key={item.name}>
                    <Table.Tr className="gn-click" onClick={() => toggle(item.name)}>
                      <Table.Td className="gn-nowrap">
                        <span className="faint gn-caret">{isOpen ? "▾" : "▸"}</span>
                        <b>{item.name}</b>
                      </Table.Td>
                      <Table.Td>{item.kind}</Table.Td>
                      <Table.Td className="faint">{item.parent || "—"}</Table.Td>
                      {MAIN.map((k) => (
                        <Table.Td key={k} className="tabular">
                          {item.attrs[k] ?? <span className="faint">—</span>}
                        </Table.Td>
                      ))}
                      <Table.Td className="tabular gn-right">{item.parameters.length}</Table.Td>
                    </Table.Tr>
                    {isOpen ? (
                      <Table.Tr className="gn-detail">
                        <Table.Td colSpan={4 + MAIN.length}>
                          {rest.length ? <Facts items={rest.map(([k, v]) => [k, v])} /> : null}
                          <Table verticalSpacing={1} withRowBorders={false} className="gn-filetable">
                            <Table.Thead>
                              <Table.Tr>
                                <Table.Th>Parameter</Table.Th>
                                <Table.Th>Value</Table.Th>
                                <Table.Th>Type</Table.Th>
                                <Table.Th>Permission</Table.Th>
                                <Table.Th>Dynamic</Table.Th>
                              </Table.Tr>
                            </Table.Thead>
                            <Table.Tbody>
                              {item.parameters.map((p) => (
                                <Table.Tr key={p.name}>
                                  <Table.Td>{p.name}</Table.Td>
                                  <Table.Td className="tabular">
                                    <b>{p.value}</b>
                                  </Table.Td>
                                  <Table.Td className="faint">{p.type}</Table.Td>
                                  <Table.Td className="faint">{p.permission}</Table.Td>
                                  <Table.Td className="faint">{p.dynamic}</Table.Td>
                                </Table.Tr>
                              ))}
                            </Table.Tbody>
                          </Table>
                        </Table.Td>
                      </Table.Tr>
                    ) : null}
                  </Fragment>
                )
              })}
            </Table.Tbody>
          </Table>
        </Panel>
      ) : null}

      {config?.links.length ? (
        <Panel title={`Links · ${config.links.length}`}>
          <Table verticalSpacing={2} className="gn-filetable">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Link</Table.Th>
                <Table.Th>From</Table.Th>
                <Table.Th>To</Table.Th>
                <Table.Th>Port</Table.Th>
                <Table.Th className="gn-right">Buffer</Table.Th>
                <Table.Th className="gn-right">Depth</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {config.links.map((link) => (
                <Table.Tr key={link.name}>
                  <Table.Td>{link.name}</Table.Td>
                  <Table.Td>{link.source}</Table.Td>
                  <Table.Td>{link.destination}</Table.Td>
                  <Table.Td className="faint">{link.destination_port}</Table.Td>
                  <Table.Td className="tabular gn-right">{Number(link.buffer_size ?? 0).toLocaleString()}</Table.Td>
                  <Table.Td className="tabular gn-right">{link.depth}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Panel>
      ) : null}
    </div>
  )
}
