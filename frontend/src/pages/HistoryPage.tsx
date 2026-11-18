import { useState } from 'react';
import { useHistory } from '../api/hooks';
import { HistoryList } from '../components/History';
import { Card, LoadError, Loading } from '../components/ui';

export function HistoryPage() {
  const [limit, setLimit] = useState(50);
  const q = useHistory(limit);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold">Configuration history</h1>
        <label className="flex items-center gap-2 text-sm text-dim">Show
          <select value={limit} onChange={(e) => setLimit(Number(e.target.value))} className="rounded-md border border-line bg-surface px-2 py-1 text-fg">
            {[50, 100, 200].map((n) => <option key={n}>{n}</option>)}
          </select>
        </label>
      </div>
      {q.isPending ? <Loading what="history" /> : q.isError ? <LoadError error={q.error} onRetry={() => void q.refetch()} /> : (
        <Card><HistoryList entries={q.data} showSource /></Card>
      )}
    </div>
  );
}
