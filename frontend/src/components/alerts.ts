/** Shared types for the alert and report endpoints. */

export type AlertRecord = {
  id: string;
  rule_key: string;
  severity: "INFO" | "WARNING" | "CRITICAL";
  state: "FIRING" | "ACKNOWLEDGED" | "RESOLVED";
  title: string;
  body: string;
  facts: Record<string, string | number | boolean | null>;
  provenance: string;
  fired_at: string;
  acknowledged_at: string | null;
  resolved_at: string | null;
  /** Stateful alerts close themselves, so the UI must not offer to close them. */
  is_stateful: boolean;
};

export type AlertRuleRecord = {
  rule_key: string;
  title: string;
  description: string;
  enabled: boolean;
  cooldown_hours: number;
  min_severity: string;
  channels: string[];
  is_stateful: boolean;
};

export type DeliveryRecord = {
  id: string;
  channel: string;
  status: "PENDING" | "SENT" | "FAILED" | "SKIPPED";
  target: string | null;
  error: string | null;
  sent_at: string | null;
  created_at: string;
};

export type ReportSection = {
  key: string;
  title: string;
  body: string;
  facts: Record<string, string | number | boolean | null>;
  provenance: string;
  caveats: string[];
};

export type ReportRecord = {
  id: string;
  period: "DAILY" | "WEEKLY" | "MONTHLY";
  period_start: string;
  period_end: string;
  generated_at: string;
  title: string;
  summary: string;
  sections: ReportSection[];
  status: "GENERATED" | "DELIVERED" | "DELIVERY_FAILED";
};

export type ChannelStatus = {
  channels: { channel: string; configured: boolean; note: string }[];
};

export const SEVERITY_TONE: Record<string, string> = {
  CRITICAL: "border-negative/40 bg-negative/10 text-negative",
  WARNING: "border-warning/40 bg-warning/10 text-warning",
  INFO: "border-border bg-surface-raised text-text-muted",
};

export const DELIVERY_TONE: Record<string, string> = {
  SENT: "text-positive",
  FAILED: "text-negative",
  // Not an error: the channel simply is not configured.
  SKIPPED: "text-text-muted",
  PENDING: "text-accent",
};

export function ruleLabel(key: string): string {
  return key.replace(/_/g, " ").toLowerCase();
}
