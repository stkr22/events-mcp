import { describe, expect, it } from 'vitest';
import type { SourceView } from '../api/types';
import { NO_FILTERS, filterSources, sortSources, summarise } from './sources';
import { changedKeys } from './diff';

const mk = (name: string, over: Partial<SourceView> = {}): SourceView => ({
  name, type: 'venue', url: `https://${name}.test`, enabled: true, config: {}, effective: null, problem: null,
  item_count: 0, last_run: null, last_success_at: null, run_request: null, updated_at: '2026-01-01T00:00:00Z', ...over,
});
const run = (status: string) => ({ status }) as SourceView['last_run'];

const all = [
  mk('b', { last_run: run('failed'), last_success_at: '2026-11-01T00:00:00Z' }),
  mk('a', { enabled: false, last_run: run('success'), last_success_at: '2026-11-10T00:00:00Z' }),
  mk('c', { type: 'youtube', last_run: run('blocked_robots') }),
  mk('d', { problem: 'bad config' }),
];

describe('source list logic', () => {
  it('filters by text, status, enabled and failing', () => {
    expect(filterSources(all, { ...NO_FILTERS, q: 'B.TEST' }).map((s) => s.name)).toEqual(['b']);
    expect(filterSources(all, { ...NO_FILTERS, type: 'youtube' }).map((s) => s.name)).toEqual(['c']);
    expect(filterSources(all, { ...NO_FILTERS, enabled: 'disabled' }).map((s) => s.name)).toEqual(['a']);
    expect(filterSources(all, { ...NO_FILTERS, failingOnly: true }).map((s) => s.name)).toEqual(['b', 'd']);
    expect(filterSources(all, { ...NO_FILTERS, status: 'never' }).map((s) => s.name)).toEqual(['d']);
  });
  it('sorts by name and by last success with never last', () => {
    expect(sortSources(all, 'name').map((s) => s.name)).toEqual(['a', 'b', 'c', 'd']);
    expect(sortSources(all, 'last_success').map((s) => s.name)).toEqual(['a', 'b', 'c', 'd']);
  });
  it('counts the summary strip', () => {
    expect(summarise(all)).toEqual({ sources: 4, failing: 2, pending: 0, blocked: 1, disabled: 1 });
  });
});

describe('changedKeys', () => {
  it('lists only changed keys and treats missing as null', () => {
    expect(changedKeys({ a: 1, b: [1, 2], c: null }, { a: 1, b: [1, 3], d: 'x' })).toEqual([
      { key: 'b', before: [1, 2], after: [1, 3] },
      { key: 'd', before: null, after: 'x' },
    ]);
  });
  it('shows everything for a creation and ignores key order', () => {
    expect(changedKeys(null, { a: 1 })).toHaveLength(1);
    expect(changedKeys({ p: { x: 1, y: 2 } }, { p: { y: 2, x: 1 } })).toEqual([]);
  });
});
