import type { SourceView } from '../api/types';

export const STATUSES = [
  'success',
  'partial',
  'failed',
  'pending_extraction',
  'blocked_robots',
  'running',
  'skipped',
] as const;

export interface Filters {
  q: string;
  type: string;
  status: string;
  enabled: 'all' | 'enabled' | 'disabled';
  failingOnly: boolean;
}
export type SortKey = 'name' | 'last_success';

export const NO_FILTERS: Filters = { q: '', type: '', status: '', enabled: 'all', failingOnly: false };

export const isFailing = (s: SourceView): boolean => s.last_run?.status === 'failed' || s.problem !== null;

export function filterSources(list: SourceView[], f: Filters): SourceView[] {
  const q = f.q.trim().toLowerCase();
  return list.filter((s) => {
    if (q && !`${s.name} ${s.url}`.toLowerCase().includes(q)) return false;
    if (f.type && s.type !== f.type) return false;
    if (f.status) {
      const st = s.last_run?.status ?? 'never';
      if (st !== f.status) return false;
    }
    if (f.enabled === 'enabled' && !s.enabled) return false;
    if (f.enabled === 'disabled' && s.enabled) return false;
    if (f.failingOnly && !isFailing(s)) return false;
    return true;
  });
}

/** Name ascending, or last success newest first with never-succeeded sources last. */
export function sortSources(list: SourceView[], key: SortKey): SourceView[] {
  const out = [...list];
  if (key === 'name') return out.sort((a, b) => a.name.localeCompare(b.name));
  const t = (s: SourceView) => (s.last_success_at ? Date.parse(s.last_success_at) : Number.NEGATIVE_INFINITY);
  return out.sort((a, b) => t(b) - t(a) || a.name.localeCompare(b.name));
}

export function summarise(list: SourceView[]) {
  const status = (s: SourceView, v: string) => s.last_run?.status === v;
  return {
    sources: list.length,
    failing: list.filter(isFailing).length,
    pending: list.filter((s) => status(s, 'pending_extraction')).length,
    blocked: list.filter((s) => status(s, 'blocked_robots')).length,
    disabled: list.filter((s) => !s.enabled).length,
  };
}
