/** Shared types for the analytics endpoints. */

export type RatePayload = {
  value: number | null;
  percent: number | null;
  denominator: "IMPRESSIONS" | "FOLLOWERS" | "NONE";
  denominator_value: number | null;
  provenance: string;
  description: string;
};

export type PostRow = {
  post_id: string;
  x_post_id: string;
  text: string;
  posted_at: string;
  engagement: number;
  impressions: number | null;
  impressions_available: boolean;
  unavailable_reason: string | null;
  engagement_rate: RatePayload;
  percentile: number | null;
  format: {
    has_media: boolean;
    has_link: boolean;
    is_thread: boolean;
    char_count: number;
    post_type: string;
  };
};

export type PostsResponse = {
  top: PostRow[];
  bottom: PostRow[];
  total_posts: number;
  comparable_posts: number;
  excluded_no_impressions: number;
};

export type Bucket = {
  label: string;
  sample_size: number;
  median_performance: number;
  is_reliable: boolean;
  status: string;
};

export type TimingResponse = {
  timezone: string;
  total_posts: number;
  is_reliable: boolean;
  recommendation: string;
  by_hour: Bucket[];
  by_weekday: Bucket[];
  best_hours: Bucket[];
  worst_hours: Bucket[];
  caveats: string[];
};

export type FormatsResponse = {
  total_posts: number;
  is_reliable: boolean;
  groups: Bucket[];
  best: string | null;
  worst: string | null;
  caveats: string[];
};

export type AttributionResponse = {
  provenance: string;
  confidence: "NONE" | "LOW" | "MODERATE" | "GOOD";
  is_usable: boolean;
  summary: string;
  hours_of_history: number;
  total_observed_growth: number;
  total_explained: number;
  unexplained: number;
  caveats: string[];
  contributions: {
    post_id: string;
    posted_at: string;
    estimate: number;
    ci_low: number;
    ci_high: number;
    share_of_explained: number;
    is_distinguishable: boolean;
  }[];
};

export type RevenueResponse = {
  provenance: string;
  note: string;
  currency: string;
  total: number;
  total_minor: number;
  entry_count: number;
  growth_ratio: number | null;
  by_source: {
    source_type: string;
    total_minor: number;
    entry_count: number;
    share: number;
  }[];
  by_month: { month: string; total_minor: number; entry_count: number }[];
  best_source: string | null;
  per_post: {
    post_id: string;
    revenue_minor: number;
    impressions: number | null;
    rpm_minor: number | null;
    rpm_available: boolean;
    reason_unavailable: string | null;
  }[];
  caveats: string[];
};

export function money(minor: number, currency = "USD"): string {
  return new Intl.NumberFormat("en", { style: "currency", currency }).format(minor / 100);
}

export function formatRate(rate: RatePayload): string {
  // An unavailable rate must never render as 0%.
  return rate.percent === null ? "—" : `${rate.percent.toFixed(2)}%`;
}
