import { useState, useCallback, useEffect, useRef } from "react";
import { Activity, CheckCircle2, XCircle, AlertTriangle, ExternalLink } from "lucide-react";
import TaskHistory from "./TaskHistory";
import PipelineFlow from "./PipelineFlow";
import TaskIntake from "./TaskIntake";
import { createTask, getTimeline, getReport, getRecentTasks, getMetrics } from "./api";
import { PIPELINE_NODES, NODE_STATUS } from "./pipelineConfig";
import "./App.css";

const POLL_INTERVAL_MS = 1500;

function buildNodeStates(timeline, overallStatus) {
  const states = {};
  const seen = new Set();

  for (const entry of timeline?.timeline || []) {
    seen.add(entry.step);
    let status = NODE_STATUS.SUCCESS;
    if (entry.status === "failed") status = NODE_STATUS.FAILED;
    else if (entry.status === "skipped") status = NODE_STATUS.SKIPPED;
    else if (entry.status === "retrying") status = NODE_STATUS.RETRYING;
    else if (entry.status === "failed_criteria") status = NODE_STATUS.FAILED;

    states[entry.step] = {
      status,
      detail: entry.detail || "",
      duration_ms: entry.duration_ms,
    };
  }

  if (overallStatus === "running") {
    for (const node of PIPELINE_NODES) {
      if (node.conditional) continue;
      if (!seen.has(node.id)) {
        states[node.id] = { status: NODE_STATUS.RUNNING, detail: "" };
        break;
      }
    }
  }

  return states;
}

function StatusIcon({ status }) {
  if (status === "success") return <CheckCircle2 size={16} color="var(--accent-green)" />;
  if (status === "failed") return <XCircle size={16} color="var(--accent-red)" />;
  if (status === "partial") return <AlertTriangle size={16} color="var(--accent-amber)" />;
  return <Activity size={16} color="var(--accent-amber)" className="spin" />;
}

function gradeClass(grade) {
  return `run-summary__grade-${grade}`;
}

function RunSummary({ report }) {
  if (!report) return null;
  const { status, qualityScore, pullRequest, codeReview, criteriaVerification, testResult, rollback } = report;

  const variant =
    status === "success" ? "success" :
    status === "failed" ? "failed" : "partial";

  const title =
    status === "success" ? "Pipeline completed" :
    status === "failed" ? "Pipeline failed" :
    "Completed with issues";

  return (
    <div className={`run-summary run-summary--${variant}`}>
      <div className="run-summary__title">
        <StatusIcon status={status} />
        {title}
      </div>

      <div className="run-summary__grid">
        {qualityScore && (
          <div className="run-summary__stat">
            <div className="run-summary__stat-label">Quality score</div>
            <div className={`run-summary__stat-value ${gradeClass(qualityScore.grade)}`}>
              {qualityScore.total} · {qualityScore.grade}
            </div>
          </div>
        )}
        {testResult && (
          <div className="run-summary__stat">
            <div className="run-summary__stat-label">Tests</div>
            <div className="run-summary__stat-value">
              {testResult.status} ({testResult.durationSeconds?.toFixed?.(1)}s)
            </div>
          </div>
        )}
        {criteriaVerification && (
          <div className="run-summary__stat">
            <div className="run-summary__stat-label">Criteria met</div>
            <div className="run-summary__stat-value">
              {(criteriaVerification.results?.length || 0) - criteriaVerification.unsatisfiedCount}
              /{criteriaVerification.results?.length || 0}
            </div>
          </div>
        )}
        {codeReview && (
          <div className="run-summary__stat">
            <div className="run-summary__stat-label">Review issues</div>
            <div className="run-summary__stat-value">{codeReview.issues?.length || 0}</div>
          </div>
        )}
      </div>

      {codeReview?.issues?.length > 0 && (
        <div className="run-summary__issues">
          {codeReview.issues.map((issue, i) => (
            <div className="run-summary__issue" key={i}>
              <span className={`run-summary__issue-severity severity-${issue.severity}`}>
                {issue.severity}
              </span>
              <span>
                <strong>{issue.file}</strong> — {issue.description}
              </span>
            </div>
          ))}
        </div>
      )}

      {rollback?.performed && (
        <div className="run-summary__issues">
          <div className="run-summary__issue">
            <span className="run-summary__issue-severity severity-warning">rollback</span>
            <span>
              Branch <code>{rollback.branch}</code> {rollback.success ? "deleted" : "could not be deleted"} — {rollback.reason}
            </span>
          </div>
        </div>
      )}

      {pullRequest?.url && (
        <a className="run-summary__pr-link" href={pullRequest.url} target="_blank" rel="noreferrer">
          <ExternalLink size={13} />
          PR #{pullRequest.number} — {pullRequest.title}
        </a>
      )}
    </div>
  );
}

function stepToTimeline(step) {
  return {
    step: step.step,
    status: step.status === "started" ? "completed" : step.status,
    detail: step.detail,
  };
}

export default function App() {
  const [tasks, setTasks] = useState([]);
  const [activeTraceId, setActiveTraceId] = useState(null);
  const [nodeStates, setNodeStates] = useState({});
  const [overallStatus, setOverallStatus] = useState(null);
  const [report, setReport] = useState(null);
  const [taskMeta, setTaskMeta] = useState(null);
  const [error, setError] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const pollRef = useRef(null);

  const refreshTasks = useCallback(async () => {
    const [{ tasks: recent }, m] = await Promise.all([getRecentTasks(20), getMetrics()]);
    setTasks(recent || []);
    setMetrics(m);
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [{ tasks: recent }, m] = await Promise.all([getRecentTasks(20), getMetrics()]);
      if (cancelled) return;
      setTasks(recent || []);
      setMetrics(m);
    })();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!activeTraceId) return;

    let cancelled = false;

    async function poll() {
      try {
        const timeline = await getTimeline(activeTraceId);
        if (cancelled) return;

        if (timeline.status === "running") {
          setOverallStatus("running");
          setNodeStates(buildNodeStates(timeline, "running"));
        } else {
          setOverallStatus(timeline.status);
          setNodeStates(buildNodeStates(timeline, timeline.status));
          const fullReport = await getReport(activeTraceId);
          if (cancelled) return;
          setReport(fullReport);
          clearInterval(pollRef.current);
          refreshTasks();
        }
      } catch (e) {
        if (!cancelled) setError(`Could not reach backend: ${e.message}`);
      }
    }

    poll();
    pollRef.current = setInterval(poll, POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      clearInterval(pollRef.current);
    };
  }, [activeTraceId, refreshTasks]);

  const handleSubmit = useCallback(async (answers) => {
    setError(null);
    setSubmitting(true);
    setReport(null);
    setNodeStates({});
    setOverallStatus("running");
    setTaskMeta({ taskId: answers.taskId, title: answers.title });

    try {
      const res = await createTask(answers);
      setActiveTraceId(res.traceId);
    } catch (e) {
      setError(e.message);
      setOverallStatus(null);
    } finally {
      setSubmitting(false);
    }
  }, []);

  const handleSelectTask = useCallback(async (traceId) => {
    clearInterval(pollRef.current);
    setActiveTraceId(traceId);
    setReport(null);
    setOverallStatus("running");
    setNodeStates({});

    const fullReport = await getReport(traceId);
    if (fullReport) {
      setReport(fullReport);
      setOverallStatus(fullReport.status);
      setTaskMeta({ taskId: fullReport.taskId, title: fullReport.taskId });
      setNodeStates(buildNodeStates({ timeline: fullReport.pipeline?.steps?.map(stepToTimeline) }, fullReport.status));
    }
  }, []);

  const handleNewTask = useCallback(() => {
    clearInterval(pollRef.current);
    setActiveTraceId(null);
    setReport(null);
    setOverallStatus(null);
    setNodeStates({});
    setTaskMeta(null);
    setError(null);
  }, []);

  const hasActivePipeline = activeTraceId !== null;

  return (
    <div className="app">
      <TaskHistory
        tasks={tasks}
        activeTraceId={activeTraceId}
        onSelect={handleSelectTask}
        onNewTask={handleNewTask}
        metrics={metrics}
      />

      <main className="main">
        {hasActivePipeline ? (
          <>
            <div className="main__header">
              <div>
                <div className="main__header-title">{taskMeta?.title || "Pipeline run"}</div>
              </div>
              <div className="main__header-meta">
                {taskMeta?.taskId} · {activeTraceId?.slice(0, 8)}
              </div>
            </div>
            <PipelineFlow nodeStates={nodeStates} />
            {report && (
              <div style={{ padding: "0 32px 32px", display: "flex", justifyContent: "center" }}>
                <div style={{ width: "100%", maxWidth: 620 }}>
                  <RunSummary report={report} />
                </div>
              </div>
            )}
          </>
        ) : (
          <div className="main__empty">
            <div className="main__empty-icon">
              <Activity size={26} strokeWidth={2} />
            </div>
            <div className="main__empty-title">No active run</div>
            <div className="main__empty-sub">
              Answer the questions on the right to describe a task, then launch
              the pipeline to watch each agent run in real time.
            </div>
          </div>
        )}
      </main>

      <div style={{ display: "flex", flexDirection: "column" }}>
        {error && <div className="error-banner">{error}</div>}
        <TaskIntake onSubmit={handleSubmit} disabled={submitting || overallStatus === "running"} />
      </div>
    </div>
  );
}
