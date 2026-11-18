import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from './endpoints';
import type { DefaultsBody, RunMode, SourceBody } from './types';

export const useMe = () => useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity });
export const useOptions = () => useQuery({ queryKey: ['options'], queryFn: api.options, staleTime: Infinity });
export const useSources = () => useQuery({ queryKey: ['sources'], queryFn: api.listSources, refetchInterval: 30_000 });
export const useSource = (name: string) =>
  useQuery({ queryKey: ['source', name], queryFn: () => api.getSource(name) });
export const useDefaults = () => useQuery({ queryKey: ['defaults'], queryFn: api.getDefaults });
export const useHistory = (limit: number) =>
  useQuery({ queryKey: ['history', limit], queryFn: () => api.history(limit) });

/** Mutations never write the cache themselves; they invalidate and let the server answer again. */
function useInvalidate() {
  const qc = useQueryClient();
  return () => Promise.all([qc.invalidateQueries({ queryKey: ['sources'] }), qc.invalidateQueries({ queryKey: ['source'] }), qc.invalidateQueries({ queryKey: ['history'] }), qc.invalidateQueries({ queryKey: ['defaults'] })]);
}

export function useCreateSource() {
  const invalidate = useInvalidate();
  return useMutation({ mutationFn: (b: SourceBody) => api.createSource(b), onSuccess: invalidate });
}
export function useUpdateSource(name: string) {
  const invalidate = useInvalidate();
  return useMutation({ mutationFn: (b: SourceBody) => api.updateSource(name, b), onSuccess: invalidate });
}
export function useSetEnabled() {
  const invalidate = useInvalidate();
  return useMutation({ mutationFn: (v: { name: string; enabled: boolean }) => api.setEnabled(v.name, v.enabled), onSuccess: invalidate });
}
export function useDeleteSource() {
  const invalidate = useInvalidate();
  return useMutation({ mutationFn: (name: string) => api.deleteSource(name), onSuccess: invalidate });
}
export function useRunSource() {
  const invalidate = useInvalidate();
  return useMutation({ mutationFn: (v: { name: string; mode: RunMode }) => api.runSource(v.name, v.mode), onSuccess: invalidate });
}
export function usePutDefaults() {
  const invalidate = useInvalidate();
  return useMutation({ mutationFn: (b: DefaultsBody) => api.putDefaults(b), onSuccess: invalidate });
}
