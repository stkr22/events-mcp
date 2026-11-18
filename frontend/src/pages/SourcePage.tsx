import { useEffect, useState, type ReactNode } from 'react';
import { ApiError } from '../api/client';
import { useCreateSource, useDefaults, useDeleteSource, useOptions, useRunSource, useSetEnabled, useSource, useUpdateSource } from '../api/hooks';
import { FALLBACK_OPTIONS, type RunMode, type SourceDetail } from '../api/types';
import { HistoryList } from '../components/History';
import { SourceFields, type Inherited } from '../components/SourceFields';
import { Badge, Banner, Button, Card, ConfirmDialog, Link, LoadError, Loading, StatusBadge, useToast } from '../components/ui';
import { mapApiError, type MappedErrors } from '../lib/errors';
import {
  SOURCE_FIELDS, applyTypePreset, emptySourceForm, formFromConfig, isDirty, toSourcePayload, validateSourceForm, type SourceForm,
} from '../lib/form';
import { absoluteTime, formatDuration, formatInterval, relativeTime } from '../lib/format';
import { navigateNow, setDirty, sourceHref } from '../lib/route';

export function NewSourcePage() {
  return <Editor key="new" />;
}

export function SourceEditPage({ name }: { name: string }) {
  const q = useSource(name);
  if (q.isPending) return <Loading what="source" />;
  if (q.isError) return <LoadError error={q.error} onRetry={() => void q.refetch()} />;
  return <Editor key={name} source={q.data} />;
}

function Editor({ source }: { source?: SourceDetail }) {
  const creating = source === undefined;
  const toast = useToast();
  const options = useOptions().data ?? FALLBACK_OPTIONS;
  const defaults = useDefaults().data;
  const create = useCreateSource();
  const update = useUpdateSource(source?.name ?? '');

  const serverForm = source ? formFromConfig(source.config) : emptySourceForm();
  const [form, setForm] = useState<SourceForm>(serverForm);
  const [initial, setInitial] = useState<SourceForm>(serverForm);
  const [seenAt, setSeenAt] = useState(source?.updated_at);
  const [errors, setErrors] = useState<MappedErrors>({ fields: {}, banner: null });
  const dirty = isDirty(form, initial);

  // A newer copy from the server replaces the form only when there is nothing to lose.
  if (source && source.updated_at !== seenAt) {
    setSeenAt(source.updated_at);
    if (!dirty) {
      setForm(serverForm);
      setInitial(serverForm);
    }
  }

  useEffect(() => {
    setDirty(dirty);
    return () => setDirty(false);
  }, [dirty]);
  useEffect(() => {
    if (!dirty) return;
    const h = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener('beforeunload', h);
    return () => window.removeEventListener('beforeunload', h);
  }, [dirty]);

  const eff = source?.effective;
  const inherited: Inherited = {
    interval: eff?.interval ?? defaults?.interval ?? '',
    city: eff?.city ?? defaults?.city ?? '',
    timezone: eff?.timezone ?? defaults?.timezone ?? '',
    render: eff?.render ?? defaults?.render ?? '',
    extractor: eff?.extractor ?? defaults?.extractor ?? '',
    kind: eff?.kind ?? (form.type === 'shop' ? 'sale' : 'event'),
    rps: defaults ? String(defaults.politeness.rps) : '1',
  };

  const patch = (p: Partial<SourceForm>) => setForm((f) => ({ ...f, ...p }));
  const setType = (t: SourceForm['type']) => setForm((f) => applyTypePreset(f, t));

  const save = () => {
    const local = validateSourceForm(form, creating);
    if (Object.keys(local).length) {
      setErrors({ fields: local, banner: 'Some fields need attention before this can be saved.' });
      return;
    }
    setErrors({ fields: {}, banner: null });
    const body = toSourcePayload(form, defaults ? { defaultRps: defaults.politeness.rps } : {});
    const fail = (err: unknown) => {
      if (creating && err instanceof ApiError && err.status === 409) {
        setErrors({ fields: { name: 'A source with this name already exists.' }, banner: null });
      } else {
        setErrors(mapApiError(err, SOURCE_FIELDS));
      }
      toast('bad', 'Not saved.');
    };
    if (creating) {
      create.mutate(body, {
        onSuccess: (s) => { toast('ok', `Created ${s.name}.`); navigateNow(sourceHref(s.name)); },
        onError: fail,
      });
    } else {
      update.mutate(body, {
        onSuccess: () => { setInitial(form); toast('ok', 'Saved.'); },
        onError: fail,
      });
    }
  };
  const saving = create.isPending || update.isPending;

  return (
    <div className="space-y-4">
      {source ? <Header source={source} /> : (
        <div>
          <p className="text-sm"><Link href="#/">Sources</Link> / New</p>
          <h1 className="text-xl font-semibold">New source</h1>
        </div>
      )}
      {source?.problem && <Banner>Invalid configuration: {source.problem}</Banner>}
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <form className="space-y-4" onSubmit={(e) => { e.preventDefault(); save(); }} noValidate>
          {errors.banner && <Banner>{errors.banner}</Banner>}
          <SourceFields form={form} patch={patch} setType={setType} errors={errors.fields} options={options} inherited={inherited} creating={creating} />
          <div className="sticky bottom-0 -mx-1 flex flex-wrap items-center gap-2 border-t border-line bg-bg px-1 py-3">
            <Button type="submit" variant="primary" disabled={saving || (!creating && !dirty)}>{saving ? 'Saving…' : creating ? 'Create source' : 'Save'}</Button>
            {!creating && <Button disabled={!dirty || saving} onClick={() => { setForm(initial); setErrors({ fields: {}, banner: null }); }}>Discard changes</Button>}
            {dirty && <Badge tone="warn">Unsaved changes</Badge>}
          </div>
        </form>
        {source && <aside className="space-y-4 lg:order-last"><Effective source={source} /></aside>}
      </div>
      {source && <Tabs source={source} />}
    </div>
  );
}

function Effective({ source }: { source: SourceDetail }) {
  const e = source.effective;
  const rows: [string, ReactNode][] = e
    ? [
        ['Interval', `${formatInterval(e.interval)} (${e.interval})`], ['City', e.city ?? '(none)'], ['Timezone', e.timezone],
        ['Render', e.render], ['Extractor', e.extractor], ['Kind', e.kind],
        ['Hosts', e.hosts.join(', ') || '(none)'], ['Primary URL', <span className="break-all font-mono">{e.primary_url}</span>],
      ]
    : [];
  return (
    <Card title="Effective configuration">
      {e ? (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-sm">
          {rows.map(([k, v]) => (<div key={k} className="contents"><dt className="text-dim">{k}</dt><dd className="min-w-0 break-words">{v}</dd></div>))}
        </dl>
      ) : <p className="text-dim">Not available: the stored configuration is invalid.</p>}
      <p className="mt-3 text-xs text-dim">Read-only: the stored values with defaults applied. Updated {relativeTime(source.updated_at)}.</p>
    </Card>
  );
}

function Header({ source }: { source: SourceDetail }) {
  const toast = useToast();
  const setEnabled = useSetEnabled();
  const run = useRunSource();
  const del = useDeleteSource();
  const [confirm, setConfirm] = useState<'delete' | 'full' | null>(null);
  const fail = (err: unknown) => toast('bad', err instanceof Error ? err.message : 'failed');

  const doRun = (mode: RunMode) =>
    run.mutate({ name: source.name, mode }, {
      onSuccess: () => toast('ok', mode === 'full' ? 'Re-extraction requested; the crawler picks it up on its next 15-minute tick.' : 'Check requested; the crawler picks it up on its next 15-minute tick.'),
      onError: fail,
    });

  return (
    <div className="space-y-3">
      <div>
        <p className="text-sm"><Link href="#/">Sources</Link> / {source.name}</p>
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="break-all text-xl font-semibold">{source.name}</h1>
          <Badge>{source.type}</Badge>
          <Badge tone={source.enabled ? 'ok' : 'neutral'}>{source.enabled ? 'enabled' : 'disabled'}</Badge>
          <StatusBadge status={source.last_run?.status} error={source.last_run?.error} />
          {source.run_request && <Badge tone="info" title="The crawler will pick this up on its next tick">run requested: {source.run_request}</Badge>}
        </div>
        <p className="text-sm text-dim">
          {source.item_count} stored items · last success{' '}
          <time dateTime={source.last_success_at ?? undefined} title={absoluteTime(source.last_success_at)}>{relativeTime(source.last_success_at)}</time>
        </p>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button disabled={setEnabled.isPending} onClick={() => setEnabled.mutate({ name: source.name, enabled: !source.enabled }, { onSuccess: () => toast('ok', source.enabled ? 'Disabled.' : 'Enabled.'), onError: fail })}>
          {source.enabled ? 'Disable' : 'Enable'}
        </Button>
        <Button disabled={run.isPending} onClick={() => doRun('due')}>Check now</Button>
        <Button disabled={run.isPending} onClick={() => setConfirm('full')}>Re-extract now</Button>
        <Button variant="danger" onClick={() => setConfirm('delete')}>Delete</Button>
      </div>
      <p className="text-xs text-dim">Check now crawls when the page is due. Requests are picked up on the crawler&apos;s next 15-minute tick, not instantly.</p>

      <ConfirmDialog open={confirm === 'full'} title="Re-extract now?" confirmLabel="Request re-extraction" busy={run.isPending}
        onCancel={() => setConfirm(null)} onConfirm={() => { setConfirm(null); doRun('full'); }}>
        <p>This ignores the unchanged-page check, so the page is read and sent to the extractor again even if it did not change. Use it after editing the intent.</p>
        <p>The crawler picks the request up on its next 15-minute tick. It may use LLM calls.</p>
      </ConfirmDialog>
      <ConfirmDialog open={confirm === 'delete'} title={`Delete ${source.name}?`} confirmLabel="Delete source" danger busy={del.isPending}
        onCancel={() => setConfirm(null)}
        onConfirm={() => del.mutate(source.name, {
          onSuccess: () => { toast('ok', `Deleted ${source.name}.`); navigateNow('#/'); },
          onError: (e) => { setConfirm(null); fail(e); },
        })}>
        <p>This removes the source configuration.</p>
        <p>The server refuses when the source still has stored items ({source.item_count} now). In that case disable the source instead; it stops crawling and keeps its data.</p>
      </ConfirmDialog>
    </div>
  );
}

function Tabs({ source }: { source: SourceDetail }) {
  const [tab, setTab] = useState<'runs' | 'history'>('runs');
  const tabCls = (t: string) => `border-b-2 px-3 py-2 text-sm font-medium ${tab === t ? 'border-accent text-fg' : 'border-transparent text-dim hover:text-fg'}`;
  return (
    <section>
      <div role="tablist" aria-label="Runs and history" className="flex border-b border-line">
        <button role="tab" type="button" id="tab-runs" aria-selected={tab === 'runs'} aria-controls="panel-runs" className={tabCls('runs')} onClick={() => setTab('runs')}>Recent runs ({source.runs.length})</button>
        <button role="tab" type="button" id="tab-history" aria-selected={tab === 'history'} aria-controls="panel-history" className={tabCls('history')} onClick={() => setTab('history')}>History ({source.history.length})</button>
      </div>
      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`} className="rounded-b-lg border border-t-0 border-line bg-surface p-4">
        {tab === 'runs' ? <Runs source={source} /> : <HistoryList entries={source.history} showSource={false} />}
      </div>
    </section>
  );
}

function Runs({ source }: { source: SourceDetail }) {
  if (!source.runs.length) return <p className="text-dim">This source has not run yet.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[44rem] text-left text-sm">
        <thead className="text-xs text-dim">
          <tr>{['Started', 'Duration', 'Status', 'Extractor', 'LLM', 'New', 'Updated', 'Touched', 'Rejected', 'Error'].map((h) => <th key={h} scope="col" className="px-2 py-1.5 font-medium">{h}</th>)}</tr>
        </thead>
        <tbody>
          {source.runs.map((r) => (
            <tr key={r.id} className="border-t border-line align-top">
              <td className="whitespace-nowrap px-2 py-1.5"><time dateTime={r.started_at} title={absoluteTime(r.started_at)}>{relativeTime(r.started_at)}</time></td>
              <td className="whitespace-nowrap px-2 py-1.5">{formatDuration(r.started_at, r.finished_at)}</td>
              <td className="px-2 py-1.5"><StatusBadge status={r.status} error={r.error} /></td>
              <td className="px-2 py-1.5">{r.extractor ?? '–'}</td>
              <td className="whitespace-nowrap px-2 py-1.5">{r.llm_provider ? `${r.llm_provider} × ${r.llm_calls}` : r.llm_calls || '–'}</td>
              <td className="px-2 py-1.5 tabular-nums">{r.items_new}</td>
              <td className="px-2 py-1.5 tabular-nums">{r.items_updated}</td>
              <td className="px-2 py-1.5 tabular-nums">{r.items_touched}</td>
              <td className="px-2 py-1.5 tabular-nums">{r.items_rejected}</td>
              <td className="max-w-[22rem] break-words px-2 py-1.5 text-bad">{r.error}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
