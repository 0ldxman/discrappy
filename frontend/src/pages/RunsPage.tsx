import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { useToast } from "../ui/toast";
import type { RunSummary } from "../types";

const STATUS: Record<RunSummary["status"], { cls: string; label: string }> = {
  running: { cls: "run", label: "идёт" },
  done: { cls: "ok", label: "готово" },
  stopped: { cls: "warn", label: "остановлен" },
  error: { cls: "err", label: "ошибка" },
};

export default function RunsPage() {
  const toast = useToast();
  const qc = useQueryClient();
  const { data: runs, isLoading } = useQuery({ queryKey: ["runs"], queryFn: api.getRuns });

  const del = useMutation({
    mutationFn: (id: string) => api.deleteRun(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["runs"] }); toast("Прогон удалён", "ok"); },
    onError: (e) => toast((e as Error).message, "err"),
  });

  return (
    <div className="stack">
      <h1>Прогоны</h1>
      {isLoading && <div className="card"><span className="spinner" /> Загрузка…</div>}
      {runs && runs.length === 0 && <div className="card empty">Пока нет ни одного прогона. Запусти скрэппинг.</div>}
      {runs && runs.length > 0 && (
        <div className="table-wrap">
          <table className="log-table">
            <thead>
              <tr><th>Дата</th><th>Каналы</th><th>Реплик</th><th>Статус</th><th></th></tr>
            </thead>
            <tbody>
              {runs.map((r) => {
                const st = STATUS[r.status] ?? STATUS.done;
                return (
                  <tr key={r.id}>
                    <td className="ts">{new Date(r.created_at).toLocaleString()}</td>
                    <td><Link to={`/runs/${r.id}`}>{r.title || r.channels.map((c) => c.name).join(", ") || r.id.slice(0, 8)}</Link></td>
                    <td>{r.message_count}</td>
                    <td><span className={`badge ${st.cls}`}>{st.label}</span></td>
                    <td className="actions">
                      <Link className="btn sm" to={`/runs/${r.id}`}>Открыть</Link>{" "}
                      <button className="btn sm danger" onClick={() => confirm("Удалить прогон и все его сообщения?") && del.mutate(r.id)}>Удалить</button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
