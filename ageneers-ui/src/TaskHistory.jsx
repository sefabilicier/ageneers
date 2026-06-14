import { CheckCircle2, XCircle, Loader2, AlertTriangle, Plus, Activity } from "lucide-react";

const STATUS_ICON = {
  success: { Icon: CheckCircle2, color: "var(--accent-green)" },
  failed:  { Icon: XCircle, color: "var(--accent-red)" },
  partial: { Icon: AlertTriangle, color: "var(--accent-amber)" },
  running: { Icon: Loader2, color: "var(--accent-amber)", spin: true },
};

function relativeTime(iso) {
  if (!iso) return "";
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.round(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}

export default function TaskHistory({ tasks, activeTraceId, onSelect, onNewTask, metrics }) {
  return (
    <div className="history">
      <div className="history__brand">
        <div className="history__brand-mark">
          <Activity size={18} strokeWidth={2.5} />
        </div>
        <div>
          <div className="history__brand-title">ageneers</div>
          <div className="history__brand-sub">Pipeline Console</div>
        </div>
      </div>

      <button className="history__new" onClick={onNewTask}>
        <Plus size={14} />
        New task
      </button>

      {metrics && (
        <div className="history__metrics">
          <div className="history__metric">
            <span className="history__metric-value">{metrics.pipeline.success_rate_pct}%</span>
            <span className="history__metric-label">success rate</span>
          </div>
          <div className="history__metric">
            <span className="history__metric-value">{metrics.pipeline.avg_quality_score}</span>
            <span className="history__metric-label">avg quality</span>
          </div>
          <div className="history__metric">
            <span className="history__metric-value">{metrics.pipeline.tasks_total}</span>
            <span className="history__metric-label">total runs</span>
          </div>
        </div>
      )}

      <div className="history__section-label">Recent sessions</div>

      <div className="history__list">
        {tasks.length === 0 && (
          <div className="history__empty">
            No runs yet — launch a task to see it here.
          </div>
        )}
        {tasks.map((t) => {
          const meta = STATUS_ICON[t.status] || STATUS_ICON.running;
          const { Icon } = meta;
          const active = t.traceId === activeTraceId;
          return (
            <button
              key={t.traceId}
              className={`history__item ${active ? "history__item--active" : ""}`}
              onClick={() => onSelect(t.traceId)}
            >
              <Icon size={14} color={meta.color} className={meta.spin ? "spin" : ""} />
              <div className="history__item-body">
                <div className="history__item-title">{t.taskId}</div>
                <div className="history__item-sub">{relativeTime(t.startedAt)}</div>
              </div>
              {t.pr_url && <span className="history__item-pr">PR</span>}
            </button>
          );
        })}
      </div>
    </div>
  );
}
