/**
 * Pipeline node definitions — mirrors app/graph/pipeline.py's 12-node graph.
 *
 * Each node has:
 *   id        — matches the step name used in step_logs / timeline
 *   label     — human-readable name shown in the UI
 *   blurb     — one-line description of what the agent does
 *   icon      — lucide-react icon name
 *   group     — visual grouping for layout (helps arrange the flow)
 */

export const PIPELINE_NODES = [
  {
    id: "task_parser",
    label: "Task Parser",
    blurb: "Reads the request and extracts repo, branch & criteria",
    icon: "FileText",
    group: "intake",
  },
  {
    id: "repo_manager",
    label: "Repo Manager",
    blurb: "Clones the repository into an isolated workspace",
    icon: "FolderGit2",
    group: "intake",
  },
  {
    id: "repo_analyzer",
    label: "Repo Analyzer",
    blurb: "Detects stack & finds the most relevant files",
    icon: "Search",
    group: "intake",
  },
  {
    id: "code_writer",
    label: "Code Writer",
    blurb: "Generates the code change with an LLM",
    icon: "Code2",
    group: "generate",
  },
  {
    id: "code_reviewer",
    label: "Code Reviewer",
    blurb: "Reviews the diff for security & quality issues",
    icon: "ShieldCheck",
    group: "verify",
  },
  {
    id: "criteria_verifier",
    label: "Criteria Verifier",
    blurb: "Checks the change against acceptance criteria",
    icon: "ListChecks",
    group: "verify",
  },
  {
    id: "test_runner",
    label: "Test Runner",
    blurb: "Runs the test suite in an isolated sandbox",
    icon: "FlaskConical",
    group: "verify",
  },
  {
    id: "git_agent",
    label: "Git Agent",
    blurb: "Creates a branch, commits & pushes the change",
    icon: "GitBranch",
    group: "ship",
  },
  {
    id: "pr_agent",
    label: "PR Agent",
    blurb: "Opens a pull request for human review",
    icon: "GitPullRequest",
    group: "ship",
  },
  {
    id: "rollback_agent",
    label: "Rollback Agent",
    blurb: "Cleans up the branch if the PR could not be created",
    icon: "Undo2",
    group: "ship",
    conditional: true, // only runs on certain failure paths
  },
  {
    id: "report",
    label: "Report",
    blurb: "Builds the execution report & quality score",
    icon: "ClipboardList",
    group: "done",
  },
];

export const NODE_STATUS = {
  IDLE: "idle",
  RUNNING: "running",
  SUCCESS: "success",
  FAILED: "failed",
  SKIPPED: "skipped",
  RETRYING: "retrying",
};

/**
 * Chatbot intake questions — mirrors the fields the backend's TaskRequest
 * needs (taskId, title, repository, branch, requirement, acceptance criteria).
 */
export const INTAKE_STEPS = [
  {
    key: "title",
    prompt: "What should this task be called?",
    placeholder: "e.g. Add email validation to registration",
    type: "text",
  },
  {
    key: "repository",
    prompt: "Which repository should I work in?",
    placeholder: "https://github.com/owner/repo",
    type: "text",
  },
  {
    key: "branch",
    prompt: "Base branch? (leave blank for main)",
    placeholder: "main",
    type: "text",
    optional: true,
  },
  {
    key: "requirement",
    prompt: "Describe the requirement in detail.",
    placeholder: "Add email format validation to POST /users/register endpoint.",
    type: "textarea",
  },
  {
    key: "criteria",
    prompt: "List acceptance criteria — one per line.",
    placeholder: "Invalid email returns HTTP 400\nError message: Invalid email format\nAdd or update unit tests",
    type: "textarea",
    multiline: true,
  },
];
