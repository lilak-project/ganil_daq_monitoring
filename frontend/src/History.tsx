import { Fragment, useMemo, useState } from "react"
import { Table } from "@mantine/core"
import { Command, Panel } from "@neon"

import { humanDuration, humanSize, localTime, type Attempt, type DataFile } from "./lib"

/** The folder a file sits in: the storage actor that wrote it (cobo0, mutant…). */
function folder(file: DataFile): string {
  const cut = file.path.lastIndexOf("/")
  return cut < 0 ? "run/" : file.path.slice(0, cut)
}

/** A file, one row: where it is, how big, which part, when it was last written. */
function FileRows({ files, runDir }: { files: DataFile[]; runDir: string }) {
  return (
    <Table className="gn-filetable" verticalSpacing={2} withRowBorders={false}>
      <Table.Thead>
        <Table.Tr>
          <Table.Th w={110}>Storage</Table.Th>
          <Table.Th>Path</Table.Th>
          <Table.Th w={60}>Part</Table.Th>
          <Table.Th w={100} className="gn-right">Size</Table.Th>
          <Table.Th w={150} className="gn-right">Bytes</Table.Th>
          <Table.Th w={190}>Last written</Table.Th>
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {files.map((file) => (
          <Table.Tr key={file.path}>
            <Table.Td>{file.derived ? <span className="faint">derived</span> : folder(file)}</Table.Td>
            <Table.Td className="gn-path" title={`${runDir}/${file.path}`}>
              <span className="faint">{runDir}/</span>
              {file.path}
            </Table.Td>
            <Table.Td className="tabular">{file.derived ? "—" : file.part}</Table.Td>
            <Table.Td className="tabular gn-right">{humanSize(file.size)}</Table.Td>
            <Table.Td className="tabular gn-right faint">{file.size.toLocaleString()}</Table.Td>
            <Table.Td className="tabular">{localTime(file.modified)}</Table.Td>
          </Table.Tr>
        ))}
      </Table.Tbody>
    </Table>
  )
}

/**
 * Every run attempt, newest first. A row opens to show every file the run
 * wrote -- each storage actor's folder, each continuation part, and the merges
 * made from them afterwards -- with its full path and size.
 */
export function History({ attempts, orphans, runDir }: { attempts: Attempt[]; orphans: DataFile[]; runDir: string }) {
  const [open, setOpen] = useState<Set<string>>(new Set())
  const key = (attempt: Attempt) => `${attempt.run}@${attempt.started}`
  const withFiles = useMemo(() => attempts.filter((a) => a.files.length), [attempts])
  const toggle = (k: string) =>
    setOpen((now) => {
      const next = new Set(now)
      if (next.has(k)) next.delete(k)
      else next.add(k)
      return next
    })
  const total = attempts.reduce((sum, a) => sum + a.size, 0)

  return (
    <div className="gn-run">
      <Panel
        fill
        title={`Runs · newest first · ${attempts.length} attempts · ${humanSize(total)} written`}
        actions={
          <span className="gn-tags">
            <Command small disabled={!withFiles.length} onRun={() => setOpen(new Set(withFiles.map(key)))}>
              Open all
            </Command>
            <Command small disabled={!open.size} onRun={() => setOpen(new Set())}>
              Close all
            </Command>
          </span>
        }
      >
        <div className="gn-runs">
          <Table stickyHeader highlightOnHover verticalSpacing="xs">
            <Table.Thead>
              <Table.Tr>
                <Table.Th w={190}>Run</Table.Th>
                <Table.Th w={200}>Started</Table.Th>
                <Table.Th w={200}>Stopped</Table.Th>
                <Table.Th w={90}>Length</Table.Th>
                <Table.Th w={70} className="gn-right">Files</Table.Th>
                <Table.Th w={100} className="gn-right">Size</Table.Th>
                <Table.Th>Note</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {attempts.map((attempt) => {
                const k = key(attempt)
                const isOpen = open.has(k)
                const storages = new Set(attempt.files.filter((f) => !f.derived).map(folder)).size
                return (
                  <Fragment key={k}>
                    <Table.Tr className={attempt.files.length ? "gn-click" : undefined} onClick={() => attempt.files.length && toggle(k)}>
                      <Table.Td className="gn-nowrap">
                        <span className="gn-tags">
                          <span className="faint gn-caret">{attempt.files.length ? (isOpen ? "▾" : "▸") : " "}</span>
                          <b className="tabular">{attempt.run}</b>
                          {attempt.live ? <span className="tone-text-ok">live</span> : null}
                          {attempt.from_server_log ? (
                            <span className="faint" title="The run-control GUI never logged this run; it was recovered from the server log (rcc.txt), which is why it has no note.">
                              server log
                            </span>
                          ) : null}
                        </span>
                      </Table.Td>
                      <Table.Td className="tabular">{localTime(attempt.started)}</Table.Td>
                      <Table.Td className="tabular">{attempt.live ? <span className="faint">running</span> : localTime(attempt.stopped)}</Table.Td>
                      <Table.Td className="tabular">{humanDuration(attempt.elapsed_s)}</Table.Td>
                      <Table.Td className="tabular gn-right" title={storages ? `${storages} storage folder(s)` : undefined}>
                        {attempt.files.length || <span className="faint">—</span>}
                      </Table.Td>
                      <Table.Td className="tabular gn-right">
                        {attempt.files.length ? humanSize(attempt.size) : <span className="faint">{attempt.storage_off ? "storage off" : "no file"}</span>}
                      </Table.Td>
                      <Table.Td style={{ whiteSpace: "pre-wrap" }}>{attempt.note}</Table.Td>
                    </Table.Tr>
                    {isOpen ? (
                      <Table.Tr className="gn-detail">
                        <Table.Td colSpan={7}>
                          <FileRows files={attempt.files} runDir={runDir} />
                        </Table.Td>
                      </Table.Tr>
                    ) : null}
                  </Fragment>
                )
              })}
            </Table.Tbody>
          </Table>
        </div>
      </Panel>

      {/* Files on disk that belong to no logged attempt. Usually an aborted
          storage, but a run whose log lines were rotated away looks the same. */}
      {orphans.length > 0 ? (
        <Panel title={`${orphans.length} file(s) matching no logged run`}>
          <div className="gn-orphans">
            <FileRows files={orphans} runDir={runDir} />
          </div>
        </Panel>
      ) : null}
    </div>
  )
}
