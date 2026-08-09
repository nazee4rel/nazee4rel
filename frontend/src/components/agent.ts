/** Shared types for the agent endpoints. */

export type AgentRun = {
  id: string;
  trigger: string;
  status: "RUNNING" | "COMPLETED" | "PARTIAL" | "SKIPPED" | "FAILED";
  stage: string;
  started_at: string;
  finished_at: string | null;
  model: string | null;
  input_tokens: number;
  output_tokens: number;
  cached_input_tokens: number;
  insights_created: number;
  recommendations_created: number;
  actions_created: number;
  grounding_rejections: number;
  ingested_untrusted_content: boolean;
  summary: string | null;
  error: string | null;
  stage_log: Record<string, Record<string, unknown>>;
};

export type AgentInsight = {
  id: string;
  agent_run_id: string;
  kind: string;
  severity: "INFO" | "NOTABLE" | "URGENT";
  title: string;
  body: string;
  /** Verified against the run's evidence before storage — never the model's own numbers. */
  supporting_data: Record<string, string | number | boolean | null>;
  confidence: number | null;
  provenance: string;
  created_at: string;
};

export type AgentRecommendation = {
  id: string;
  title: string;
  rationale: string;
  supporting_data: Record<string, string | number | boolean | null>;
  predicted_metric: string;
  predicted_direction: "INCREASE" | "DECREASE" | "MAINTAIN";
  baseline_value: number | null;
  baseline_note: string | null;
  verify_after: string;
  status: "PROPOSED" | "ACCEPTED" | "DISMISSED" | "SUPERSEDED";
  verification_result: "PENDING" | "CONFIRMED" | "REFUTED" | "INCONCLUSIVE";
  observed_value: number | null;
  verification_note: string | null;
  created_at: string;
};

export type AgentAction = {
  id: string;
  action_type: string;
  autonomy_tier: "T0" | "T1" | "T2" | "T3";
  status:
    | "PENDING_APPROVAL"
    | "APPROVED"
    | "REJECTED"
    | "EXECUTED"
    | "FAILED"
    | "EXPIRED"
    | "BLOCKED";
  summary: string;
  payload: Record<string, string>;
  /** Written by the backend, not by the model: what you approve must not be attacker-authored. */
  effect: string;
  requires_approval: boolean;
  ingested_untrusted_content: boolean;
  expires_at: string | null;
  executed_at: string | null;
  result: Record<string, unknown>;
  error: string | null;
  blocked_reason: string | null;
  created_at: string;
};

export type AgentPolicy = {
  tiers: Record<string, string>;
  actions: {
    action_type: string;
    tier: string;
    effect: string;
    outward_facing: boolean;
    requires_write_flag: boolean;
  }[];
  never_implemented: string[];
  note: string;
};

export const METRIC_LABELS: Record<string, string> = {
  FOLLOWER_GROWTH_DAILY: "followers gained per day",
  ENGAGEMENT_RATE_MEDIAN: "median engagement rate",
  POSTS_PER_WEEK: "posts per week",
  IMPRESSIONS_MEDIAN: "median impressions",
};

export const VERDICT_TONE: Record<string, string> = {
  CONFIRMED: "border-positive/40 bg-positive/10 text-positive",
  REFUTED: "border-negative/40 bg-negative/10 text-negative",
  INCONCLUSIVE: "border-border bg-surface-raised text-text-muted",
  PENDING: "border-accent/30 bg-accent/10 text-accent",
};

export const SEVERITY_TONE: Record<string, string> = {
  URGENT: "border-negative/40 text-negative",
  NOTABLE: "border-warning/40 text-warning",
  INFO: "border-border text-text-muted",
};

export const RUN_STATUS_TONE: Record<string, string> = {
  COMPLETED: "text-positive",
  PARTIAL: "text-warning",
  SKIPPED: "text-text-muted",
  FAILED: "text-negative",
  RUNNING: "text-accent",
};

export const ACTION_STATUS_TONE: Record<string, string> = {
  EXECUTED: "text-positive",
  PENDING_APPROVAL: "text-accent",
  APPROVED: "text-accent",
  REJECTED: "text-text-muted",
  EXPIRED: "text-text-muted",
  BLOCKED: "text-warning",
  FAILED: "text-negative",
};

/** Format a predicted metric value for display, keeping rates as percentages. */
export function formatMetricValue(metric: string, value: number | null): string {
  if (value === null) return "—";
  if (metric === "ENGAGEMENT_RATE_MEDIAN") return `${(value * 100).toFixed(2)}%`;
  if (metric === "IMPRESSIONS_MEDIAN") return Math.round(value).toLocaleString();
  return value.toFixed(2);
}
