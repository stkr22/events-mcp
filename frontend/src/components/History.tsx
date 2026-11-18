import { useState } from 'react';
import type { HistoryEntry } from '../api/types';
import { changedKeys, show } from '../lib/diff';
import { absoluteTime, relativeTime } from '../lib/format';
import { sourceHref } from '../lib/route';
import { Badge, Link, type Tone } from './ui';

const ACTION_TONE: Record<string, Tone> = { create: 'ok', created: 'ok', delete: 'bad', deleted: 'bad', update: 'info', updated: 'info' };

export function Diff({ entry }: { entry: HistoryEntry }) {
  const changes = changedKeys(entry.before, entry.after);
  if (!changes.length) return <p className="text-dim">No field changed.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-xs">
        <thead className="text-dim">
          <tr><th className="py-1 pr-3 font-medium">Field</th><th className="py-1 pr-3 font-medium">Before</th><th className="py-1 font-medium">After</th></tr>
        </thead>
        <tbody>
          {changes.map((c) => (
            <tr key={c.key} className="border-t border-line align-top">
              <td className="py-1 pr-3 font-mono">{c.key}</td>
              <td className="max-w-[24rem] break-words py-1 pr-3 font-mono text-bad">{show(c.before)}</td>
              <td className="max-w-[24rem] break-words py-1 font-mono text-ok">{show(c.after)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function HistoryList({ entries, showSource }: { entries: HistoryEntry[]; showSource: boolean }) {
  const [open, setOpen] = useState<ReadonlySet<number>>(new Set());
  if (!entries.length) return <p className="py-4 text-dim">No configuration changes recorded.</p>;
  const toggle = (id: number) => setOpen((s) => { const n = new Set(s); if (!n.delete(id)) n.add(id); return n; });
  return (
    <ul className="divide-y divide-line">
      {entries.map((e) => (
        <li key={e.id} className="py-2">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <button type="button" aria-expanded={open.has(e.id)} aria-controls={`diff-${e.id}`} onClick={() => toggle(e.id)}
              className="rounded px-1 text-dim hover:bg-subtle" aria-label={`${open.has(e.id) ? 'Hide' : 'Show'} changes of entry ${e.id}`}>
              {open.has(e.id) ? '▾' : '▸'}
            </button>
            <Badge tone={ACTION_TONE[e.action] ?? 'neutral'}>{e.action}</Badge>
            {showSource && <Link href={sourceHref(e.source_name)} className="font-medium">{e.source_name}</Link>}
            <span className="break-all text-dim">{e.actor}</span>
            <time dateTime={e.changed_at} title={absoluteTime(e.changed_at)} className="text-dim">{relativeTime(e.changed_at)}</time>
          </div>
          {open.has(e.id) && <div id={`diff-${e.id}`} className="mt-2 pl-7"><Diff entry={e} /></div>}
        </li>
      ))}
    </ul>
  );
}
