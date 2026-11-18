import { useState } from 'react';
import { useOptions, useSetEnabled, useSources } from '../api/hooks';
import { FALLBACK_OPTIONS, type SourceView } from '../api/types';
import { Badge, Button, LoadError, Loading, Link, StatusBadge, useToast } from '../components/ui';
import { absoluteTime, formatInterval, relativeTime } from '../lib/format';
import { go, sourceHref } from '../lib/route';
import { NO_FILTERS, STATUSES, filterSources, sortSources, summarise, type Filters, type SortKey } from '../lib/sources';

const sel = 'rounded-md border border-line bg-surface px-2 py-1.5 text-sm';

function Stat({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className="min-w-[7rem] flex-1 rounded-lg border border-line bg-surface px-3 py-2">
      <div className={`text-xl font-semibold tabular-nums ${value > 0 && tone ? tone : ''}`}>{value}</div>
      <div className="text-xs text-dim">{label}</div>
    </div>
  );
}

export function SourcesPage() {
  const q = useSources();
  const options = useOptions().data ?? FALLBACK_OPTIONS;
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [sort, setSort] = useState<SortKey>('name');
  const toast = useToast();
  const setEnabled = useSetEnabled();

  if (q.isPending) return <Loading what="sources" />;
  if (q.isError) return <LoadError error={q.error} onRetry={() => void q.refetch()} />;

  const all = q.data;
  const sum = summarise(all);
  const rows = sortSources(filterSources(all, filters), sort);
  const set = (p: Partial<Filters>) => setFilters((f) => ({ ...f, ...p }));
  const toggle = (s: SourceView) =>
    setEnabled.mutate({ name: s.name, enabled: !s.enabled }, {
      onSuccess: () => toast('ok', `${s.name} ${s.enabled ? 'disabled' : 'enabled'}.`),
      onError: (e) => toast('bad', e.message),
    });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold">Sources</h1>
        <Button variant="primary" onClick={() => go('#/new')}>New source</Button>
      </div>

      <div className="flex flex-wrap gap-2" aria-label="Summary">
        <Stat label="Sources" value={sum.sources} />
        <Stat label="Failing" value={sum.failing} tone="text-bad" />
        <Stat label="Pending extraction" value={sum.pending} tone="text-info" />
        <Stat label="Blocked by robots" value={sum.blocked} tone="text-warn" />
        <Stat label="Disabled" value={sum.disabled} />
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <label className="flex min-w-[12rem] flex-1 flex-col gap-1 text-xs text-dim">Search
          <input type="search" value={filters.q} onChange={(e) => set({ q: e.target.value })} placeholder="Name or URL" className={`${sel} text-fg`} />
        </label>
        <label className="flex flex-col gap-1 text-xs text-dim">Type
          <select value={filters.type} onChange={(e) => set({ type: e.target.value })} className={`${sel} text-fg`}>
            <option value="">All</option>{options.source_types.map((t) => <option key={t}>{t}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-dim">Last run
          <select value={filters.status} onChange={(e) => set({ status: e.target.value })} className={`${sel} text-fg`}>
            <option value="">All</option><option value="never">never run</option>{STATUSES.map((s) => <option key={s} value={s}>{s.replace(/_/g, ' ')}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-dim">State
          <select value={filters.enabled} onChange={(e) => set({ enabled: e.target.value as Filters['enabled'] })} className={`${sel} text-fg`}>
            <option value="all">All</option><option value="enabled">Enabled</option><option value="disabled">Disabled</option>
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-dim">Sort by
          <select value={sort} onChange={(e) => setSort(e.target.value as SortKey)} className={`${sel} text-fg`}>
            <option value="name">Name</option><option value="last_success">Last success</option>
          </select>
        </label>
        <label className="flex items-center gap-2 pb-1.5 text-sm">
          <input type="checkbox" checked={filters.failingOnly} onChange={(e) => set({ failingOnly: e.target.checked })} className="size-4 accent-[var(--accent)]" /> Failing only
        </label>
      </div>

      <div className="overflow-x-auto rounded-lg border border-line bg-surface">
        <table className="w-full min-w-[60rem] text-left text-sm">
          <caption className="sr-only">Sources and how their last run went</caption>
          <thead className="bg-subtle text-xs text-dim">
            <tr>{['Name', 'Type', 'Enabled', 'Last run', 'Last success', 'Items', 'Interval', 'Render / extractor', 'Flags'].map((h) => <th key={h} scope="col" className="px-3 py-2 font-medium">{h}</th>)}</tr>
          </thead>
          <tbody>
            {rows.map((s) => (
              <tr key={s.name} onClick={() => go(sourceHref(s.name))} className="cursor-pointer border-t border-line align-top hover:bg-subtle">
                <td className="px-3 py-2"><Link href={sourceHref(s.name)} className="break-all font-medium">{s.name}</Link></td>
                <td className="px-3 py-2">{s.type}</td>
                <td className="px-3 py-2" onClick={(e) => e.stopPropagation()}>
                  <button type="button" role="switch" aria-checked={s.enabled} aria-label={`${s.name} enabled`} disabled={setEnabled.isPending} onClick={() => toggle(s)}
                    className={`relative inline-block h-5 w-9 rounded-full border border-line align-middle ${s.enabled ? 'bg-accent' : 'bg-subtle'}`}>
                    <span className={`absolute top-0.5 size-3.5 rounded-full bg-surface shadow ${s.enabled ? 'left-[1.1rem]' : 'left-0.5'}`} />
                  </button>
                </td>
                <td className="px-3 py-2">
                  <StatusBadge status={s.last_run?.status} error={s.last_run?.error} />
                  {s.last_run?.error && <div className="mt-0.5 max-w-[16rem] truncate text-xs text-bad" title={s.last_run.error}>{s.last_run.error}</div>}
                </td>
                <td className="whitespace-nowrap px-3 py-2"><time dateTime={s.last_success_at ?? undefined} title={absoluteTime(s.last_success_at)}>{relativeTime(s.last_success_at)}</time></td>
                <td className="px-3 py-2 tabular-nums">{s.item_count}</td>
                <td className="whitespace-nowrap px-3 py-2">{s.effective ? formatInterval(s.effective.interval) : '–'}</td>
                <td className="whitespace-nowrap px-3 py-2">{s.effective ? `${s.effective.render} / ${s.effective.extractor}` : '–'}</td>
                <td className="px-3 py-2">
                  <div className="flex flex-wrap gap-1">
                    {s.run_request && <Badge tone="info" title="The crawler will pick this up on its next tick">run requested: {s.run_request}</Badge>}
                    {s.problem && <Badge tone="bad" title={s.problem}>invalid config</Badge>}
                  </div>
                  {s.problem && <div className="mt-0.5 max-w-[16rem] truncate text-xs text-bad" title={s.problem}>{s.problem}</div>}
                </td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={9} className="px-3 py-6 text-center text-dim">{all.length ? 'No source matches these filters.' : 'No sources yet.'}</td></tr>}
          </tbody>
        </table>
      </div>
      <p className="text-xs text-dim">{rows.length} of {all.length} shown. Refreshes every 30 seconds.</p>
    </div>
  );
}
