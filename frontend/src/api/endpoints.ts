import { request } from './client';
import type {
  Defaults,
  DefaultsBody,
  HistoryEntry,
  Me,
  Options,
  RunMode,
  SourceBody,
  SourceDetail,
  SourceView,
} from './types';

const src = (name: string) => `/api/sources/${encodeURIComponent(name)}`;

export const api = {
  me: () => request<Me>('GET', '/api/me'),
  options: () => request<Options>('GET', '/api/options'),
  listSources: () => request<SourceView[]>('GET', '/api/sources'),
  getSource: (name: string) => request<SourceDetail>('GET', src(name)),
  createSource: (body: SourceBody) => request<SourceView>('POST', '/api/sources', body),
  updateSource: (name: string, body: SourceBody) => request<SourceView>('PUT', src(name), body),
  setEnabled: (name: string, enabled: boolean) =>
    request<SourceView>('POST', `${src(name)}/${enabled ? 'enable' : 'disable'}`),
  deleteSource: (name: string) => request<undefined>('DELETE', src(name)),
  runSource: (name: string, mode: RunMode) => request<SourceView>('POST', `${src(name)}/run`, { mode }),
  getDefaults: () => request<Defaults>('GET', '/api/defaults'),
  putDefaults: (body: DefaultsBody) => request<Defaults>('PUT', '/api/defaults', body),
  history: (limit: number) => request<HistoryEntry[]>('GET', `/api/history?limit=${limit}`),
  logout: () => request<{ ok: boolean }>('POST', '/auth/logout'),
};
