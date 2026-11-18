import { useSyncExternalStore } from 'react';

export type Route =
  | { page: 'sources' }
  | { page: 'source'; name: string }
  | { page: 'new' }
  | { page: 'defaults' }
  | { page: 'history' };

export function parseHash(hash: string): Route {
  const path = hash.replace(/^#\/?/, '').replace(/\/+$/, '');
  if (path === 'new') return { page: 'new' };
  if (path === 'defaults') return { page: 'defaults' };
  if (path === 'history') return { page: 'history' };
  const m = /^sources\/(.+)$/.exec(path);
  if (m?.[1]) {
    try {
      return { page: 'source', name: decodeURIComponent(m[1]) };
    } catch {
      return { page: 'sources' };
    }
  }
  return { page: 'sources' };
}

export const sourceHref = (name: string): string => `#/sources/${encodeURIComponent(name)}`;

function subscribe(cb: () => void): () => void {
  window.addEventListener('hashchange', cb);
  return () => window.removeEventListener('hashchange', cb);
}

const hashSnapshot = (): string => window.location.hash;

export function useHash(): string {
  return useSyncExternalStore(subscribe, hashSnapshot);
}

export function useRoute(): Route {
  return parseHash(useHash());
}

// --- unsaved-changes guard: links ask before leaving a dirty form -------------------------------------
let dirty = false;
let pending: string | null = null;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

export function setDirty(v: boolean): void {
  if (dirty !== v) {
    dirty = v;
    emit();
  }
}
export const isDirtyNow = (): boolean => dirty;

/** Navigate, but ask first when a form has unsaved edits. */
export function go(href: string): void {
  if (dirty) {
    pending = href;
    emit();
  } else {
    window.location.hash = href;
  }
}

export function navigateNow(href: string): void {
  dirty = false;
  pending = null;
  window.location.hash = href;
  emit();
}

export function cancelPending(): void {
  pending = null;
  emit();
}

export function usePendingNavigation(): string | null {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => pending,
  );
}
