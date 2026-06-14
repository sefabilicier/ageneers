import { useEffect, useRef } from "react";
import {
  FileText, FolderGit2, Search, Code2, ShieldCheck, ListChecks,
  FlaskConical, GitBranch, GitPullRequest, Undo2, ClipboardList,
  CheckCircle2, XCircle, Loader2, Circle, ArrowDown, SkipForward,
} from "lucide-react";
import { PIPELINE_NODES, NODE_STATUS } from "./pipelineConfig";

const ICONS = {
  FileText, FolderGit2, Search, Code2, ShieldCheck, ListChecks,
  FlaskConical, GitBranch, GitPullRequest, Undo2, ClipboardList,
};

const STATUS_META = {
  [NODE_STATUS.IDLE]:     { label: "Waiting",   color: "var(--text-muted)" },
  [NODE_STATUS.RUNNING]:  { label: "Running",   color: "var(--accent-amber)" },
  [NODE_STATUS.SUCCESS]:  { label: "Done",      color: "var(--accent-green)" },
  [NODE_STATUS.FAILED]:   { label: "Failed",    color: "var(--accent-red)" },
  [NODE_STATUS.SKIPPED]:  { label: "Skipped",   color: "var(--text-muted)" },
  [NODE_STATUS.RETRYING]: { label: "Retrying",  color: "var(--accent-purple)" },
};

/**
 * A single pipeline node card.
 * status: idle | running | success | failed | skipped | retrying
 * detail: optional one-line string shown in the terminal strip
 */
function NodeCard({ node, status, detail }) {
  const Icon = ICONS[node.icon] || Circle;
  const meta = STATUS_META[status] || STATUS_META.idle;

  const glow =
    status === NODE_STATUS.RUNNING ? "var(--shadow-glow-amber)" :
    status === NODE_STATUS.SUCCESS ? "var(--shadow-glow-green)" :
    status === NODE_STATUS.FAILED  ? "var(--shadow-glow-red)" :
    "none";

  return (
    <div
      className={`node-card status-${status}`}
      style={{ boxShadow: glow }}
    >
      <div className="node-card__top">
        <div className="node-card__icon" style={{ color: meta.color }}>
          <Icon size={18} strokeWidth={2} />
        </div>
        <div className="node-card__titles">
          <div className="node-card__label">{node.label}</div>
          <div className="node-card__blurb">{node.blurb}</div>
        </div>
        <div className="node-card__status" style={{ color: meta.color }}>
          {status === NODE_STATUS.RUNNING && <Loader2 size={15} className="spin" />}
          {status === NODE_STATUS.SUCCESS && <CheckCircle2 size={15} />}
          {status === NODE_STATUS.FAILED && <XCircle size={15} />}
          {status === NODE_STATUS.SKIPPED && <SkipForward size={14} />}
          {status === NODE_STATUS.IDLE && <Circle size={12} />}
          <span>{meta.label}</span>
        </div>
      </div>

      {detail && (
        <div className="node-card__detail">
          <span className="node-card__detail-prompt">$</span> {detail}
        </div>
      )}
    </div>
  );
}

/**
 * Renders the full pipeline as a vertical flow of node cards.
 *
 * nodeStates: { [nodeId]: { status, detail } }
 * Nodes not present in nodeStates render as "idle".
 */
export default function PipelineFlow({ nodeStates }) {
  const containerRef = useRef(null);

  // Auto-scroll to the currently running node
  useEffect(() => {
    const runningId = Object.entries(nodeStates).find(
      ([, s]) => s.status === NODE_STATUS.RUNNING
    )?.[0];
    if (!runningId || !containerRef.current) return;
    const el = containerRef.current.querySelector(`[data-node-id="${runningId}"]`);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [nodeStates]);

  // Hide rollback_agent unless it has actually been triggered
  const visibleNodes = PIPELINE_NODES.filter((n) => {
    if (!n.conditional) return true;
    const s = nodeStates[n.id];
    return s && s.status !== NODE_STATUS.IDLE;
  });

  return (
    <div className="pipeline-flow" ref={containerRef}>
      {visibleNodes.map((node, i) => {
        const state = nodeStates[node.id] || { status: NODE_STATUS.IDLE };
        return (
          <div className="pipeline-flow__item" key={node.id} data-node-id={node.id}>
            <NodeCard node={node} status={state.status} detail={state.detail} />
            {i < visibleNodes.length - 1 && (
              <div className="pipeline-flow__connector">
                <ArrowDown size={16} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
