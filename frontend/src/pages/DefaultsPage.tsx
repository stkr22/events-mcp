import { useEffect, useState } from 'react';
import { useDefaults, useOptions, usePutDefaults } from '../api/hooks';
import { FALLBACK_OPTIONS } from '../api/types';
import { Banner, Button, Card, CheckboxField, LoadError, Loading, SelectField, TextField, useToast } from '../components/ui';
import { mapApiError, fieldError, type MappedErrors } from '../lib/errors';
import { DEFAULTS_FIELDS, formFromDefaults, isDirty, toDefaultsPayload, validateDefaultsForm, type DefaultsForm } from '../lib/form';
import { setDirty } from '../lib/route';

export function DefaultsPage() {
  const q = useDefaults();
  if (q.isPending) return <Loading what="defaults" />;
  if (q.isError) return <LoadError error={q.error} onRetry={() => void q.refetch()} />;
  return <DefaultsEditor key={JSON.stringify(q.data)} initial={formFromDefaults(q.data)} />;
}

function DefaultsEditor({ initial }: { initial: DefaultsForm }) {
  const options = useOptions().data ?? FALLBACK_OPTIONS;
  const put = usePutDefaults();
  const toast = useToast();
  const [form, setForm] = useState(initial);
  const [errors, setErrors] = useState<MappedErrors>({ fields: {}, banner: null });
  const dirty = isDirty(form, initial);
  const patch = (p: Partial<DefaultsForm>) => setForm((f) => ({ ...f, ...p }));
  const err = (k: string) => fieldError(errors.fields, k);

  useEffect(() => {
    setDirty(dirty);
    return () => setDirty(false);
  }, [dirty]);

  const save = () => {
    const local = validateDefaultsForm(form);
    if (Object.keys(local).length) {
      setErrors({ fields: local, banner: 'Some fields need attention before this can be saved.' });
      return;
    }
    setErrors({ fields: {}, banner: null });
    put.mutate(toDefaultsPayload(form), {
      onSuccess: () => toast('ok', 'Defaults saved.'),
      onError: (e) => { setErrors(mapApiError(e, DEFAULTS_FIELDS)); toast('bad', 'Not saved.'); },
    });
  };

  return (
    <form className="max-w-3xl space-y-4" noValidate onSubmit={(e) => { e.preventDefault(); save(); }}>
      <h1 className="text-xl font-semibold">Defaults</h1>
      <Banner tone="info">Every source inherits these values unless it sets its own. Changing them affects all sources that leave the field empty.</Banner>
      {errors.banner && <Banner>{errors.banner}</Banner>}
      <Card title="Inherited values">
        <div className="grid gap-4 sm:grid-cols-2">
          <TextField label="City" value={form.city} onChange={(v) => patch({ city: v })} hint="Optional; used for geocoding and filtering." error={err('city')} />
          <TextField label="Timezone" value={form.timezone} onChange={(v) => patch({ timezone: v })} mono error={err('timezone')} />
          <TextField label="Interval" value={form.interval} onChange={(v) => patch({ interval: v })} mono hint="30m, 6h or 2d." error={err('interval')} />
          <SelectField label="Render" value={form.render} onChange={(v) => patch({ render: v as DefaultsForm['render'] })} options={options.renders} error={err('render')} />
          <SelectField label="Extractor" value={form.extractor} onChange={(v) => patch({ extractor: v as DefaultsForm['extractor'] })} options={options.extractors} error={err('extractor')} />
        </div>
      </Card>
      <Card title="Politeness">
        <div className="space-y-4">
          <div className="max-w-xs"><TextField label="Requests per second (per host)" value={form.rps} onChange={(v) => patch({ rps: v })} inputMode="decimal" hint="Above 0, at most 10." error={err('politeness.rps')} /></div>
          <CheckboxField label="Ignore robots.txt" checked={form.ignoreRobots} onChange={(v) => patch({ ignoreRobots: v })}
            warn="This would make every source ignore robots.txt unless it overrides it. Ignoring robots.txt is an owner decision; prefer setting it on individual sources." />
        </div>
      </Card>
      <div className="flex items-center gap-2">
        <Button type="submit" variant="primary" disabled={!dirty || put.isPending}>{put.isPending ? 'Saving…' : 'Save defaults'}</Button>
        <Button disabled={!dirty} onClick={() => { setForm(initial); setErrors({ fields: {}, banner: null }); }}>Discard changes</Button>
      </div>
    </form>
  );
}
