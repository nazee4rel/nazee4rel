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

export type GrowthScore = {
  score: number | null;
  components: Record<string, number>;
  missing: string[];
  explanation: string;
  provenance: string;
};

export type SummaryResponse = {
  window_days: number;
  followers: {
    value: number | null;
    change_7d: number | null;
    change_note: string | null;
    median_daily_change: number | null;
    baseline_reliable: boolean;
    hours_of_history: number;
    provenance: string;
  };
  engagement: {
    median_rate: number | null;
    basis: "IMPRESSIONS" | "FOLLOWERS" | "MIXED" | "NONE";
    sample_size: number;
    provenance: string;
  };
  impressions: {
    /** Null, never 0, when nothing was collected. */
    total: number | null;
    posts_counted: number;
    posts_missing: number;
    provenance: string;
  };
  revenue: {
    total_minor: number;
    currency: string;
    entry_count: number;
    provenance: string;
  };
  posts: number;
  growth_score: GrowthScore;
  trend: {
    direction: string;
    change_ratio: number | null;
    explanation: string;
    is_reliable: boolean;
  } | null;
  latest_anomaly: {
    direction: string;
    severity: string;
    z_score: number | null;
    explanation: string;
  } | null;
  top_post: {
    post_id: string;
    x_post_id: string;
    text: string;
    posted_at: string;
    engagement: number;
    impressions: number | null;
    engagement_rate: RatePayload;
  } | null;
  caveats: string[];
};

export type SeriesResponse = {
  provenance: string;
  points: { at: string; followers: number }[];
  daily: {
    day: string;
    followers: number | null;
    delta: number | null;
    posts: number;
    /** False where no snapshot exists. Must not be plotted as zero. */
    observed: boolean;
  }[];
  gaps: { start: string; end: string; hours: number }[];
  caveats: string[];
};

export type TopicsResponse = {
  topics: {
    topic: string;
    posts: number;
    median_engagement_rate: number | null;
    share_of_classified: number;
    is_reliable: boolean;
    status: string;
  }[];
  total_posts: number;
  classified_posts: number;
  unclassified_posts: number;
  best: string | null;
  worst: string | null;
  is_reliable: boolean;
  provenance: string;
  caveats: string[];
};

export type SeasonalityProfile = {
  is_reliable: boolean;
  days: { weekday: number; name: string; median: number | null; observations: number }[];
};

export type SeasonalityResponse = {
  follower_change: SeasonalityProfile;
  engagement_rate: SeasonalityProfile;
  follower_days_observed: number;
  posts_observed: number;
  minimum_per_weekday: number;
  caveats: string[];
};

/** Compact number for headline tiles: 12.4k rather than 12,431. */
export function compact(value: number): string {
  return new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(
    value,
  );
}

/** A signed change, with an explicit sign so a gain reads as a gain. */
export function signed(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toLocaleString()}`;
}

export function shortDay(day: string): string {
  return new Date(`${day}T00:00:00Z`).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}
