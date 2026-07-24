import { useQuery } from "@tanstack/react-query";
import { api } from "../api";

// Предпросмотр образца: классификация последних сообщений канала (взято/отброшено).
export default function PreviewModal({ channelId, onClose }: { channelId: string; onClose: () => void }) {
  const { data, isFetching, error } = useQuery({
    queryKey: ["preview", channelId],
    queryFn: () => api.preview({ channel_id: channelId, limit: 50 }),
    enabled: !!channelId,
  });

  return (
    <div className="overlay" onClick={onClose}>
      <div className="modal wide" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>Предпросмотр образца</h2>
          <button className="btn ghost icon" onClick={onClose}>✕</button>
        </div>
        <div className="modal-body">
          {isFetching && <div className="row"><span className="spinner" /> Загружаю…</div>}
          {error && <div className="empty">{(error as Error).message}</div>}
          {data && (
            <>
              <div className="muted small" style={{ marginBottom: 10 }}>
                Всего {data.total} · <span className="badge ok">взято {data.kept}</span>{" "}
                {Object.entries(data.dropped).map(([r, n]) => (
                  <span key={r} className="badge warn" style={{ marginLeft: 4 }}>{r}: {n}</span>
                ))}
              </div>
              <div className="table-wrap">
                <table className="log-table">
                  <thead>
                    <tr><th>Тип</th><th>Время</th><th>Имя</th><th>Текст</th><th>Вердикт</th></tr>
                  </thead>
                  <tbody>
                    {data.items.map((it, i) => (
                      <tr key={i}>
                        <td className="chat">{it.kind}</td>
                        <td className="ts">{it.ts}</td>
                        <td className="author">{it.name}</td>
                        <td className="msg">{it.text}</td>
                        <td>{it.kept
                          ? <span className="badge ok">взято</span>
                          : <span className="badge warn">{it.reason}</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
