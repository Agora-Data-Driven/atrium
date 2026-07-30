"""Stage 2 of the Agora data contract -- the `tcs` client EXPORT job (Business Quiz dashboard).

Three-stage data contract:
    Stage 1  sql/*.sql views (BigQuery)   -- quiz -> conversion -> engagement models from raw_windsor.tcs_*
    Stage 2  job/main.py  (THIS FILE)     -- assemble the `data` dict, upload tcs.json
    Stage 3  dash/dashboard.html          -- read data.* and render the diagnostic

The keys of the assembled `data` dict (and of the rows inside data["monthly"] / ["cohorts"] /
["conversion_trend"] / ["leads"]) MUST match the `data.*` keys dashboard.html reads. Renaming a key
here silently breaks the dashboard.

Self-gating freshness:
  Runs on a `*/10` tick but only rebuilds when the shared raw_windsor.tcs_* mirror tables the views
  read (GATING_TABLES) advanced past the `_freshness.json` watermark in THIS client's bucket.
  FORCE_REBUILD=1 bypasses the gate. The watermark is written ONLY after a successful upload.
"""

import json
import os
from datetime import datetime, timezone

from google.cloud import bigquery, storage

import freshness

PROJECT = "agora-data-driven"
LOC = "asia-southeast1"  # Singapore.

CLIENT = "tcs"
DATASET = f"client_{CLIENT}"
BUCKET = f"agora-data-driven-{CLIENT}-dash"
DATA_OBJECT = f"{CLIENT}.json"

# The BASE raw_windsor mirror tables the Stage 1 views read. We watermark THESE -- never a view.
# perf_meta is written by the SHARED windsor-meta-ingest job (services/ingest/meta) for the
# whole estate; the paid_media_* views read this client's slice out of it. It gates like any
# other upstream: when the nightly Meta pull advances, this export rebuilds.
GATING_TABLES = [
    "raw_windsor.tcs_quiz",
    "raw_windsor.tcs_shopify_orders",
    "raw_windsor.tcs_klaviyo_events",
    "raw_windsor.perf_meta",
]
WATERMARK_OBJECT = "_freshness.json"
LEADS_LIMIT = 800
ADS_LIMIT = 100


def _iso_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _i(v):
    return int(v) if v is not None else 0


def _f(v):
    return float(v) if v is not None else None


def _date(v):
    return v.isoformat() if v is not None and hasattr(v, "isoformat") else v


def _read_kpis(bq):
    sql = f"""
        SELECT leads, converted, conversion_rate, revenue, avg_days_to_convert,
               avg_opens, avg_clicks, leads_this_year, converted_this_year,
               open_rate_this_year, open_rate_prior, click_rate_this_year, click_rate_prior
        FROM `{PROJECT}.{DATASET}.kpi_overview`
    """
    rows = list(bq.query(sql, location=LOC).result())
    if not rows:
        return {k: 0 for k in ("leads", "converted", "leads_this_year", "converted_this_year")} | \
               {k: None for k in ("conversion_rate", "revenue", "avg_days_to_convert", "avg_opens",
                                  "avg_clicks", "open_rate_this_year", "open_rate_prior",
                                  "click_rate_this_year", "click_rate_prior")}
    r = rows[0]
    return {
        "leads": _i(r["leads"]),
        "converted": _i(r["converted"]),
        "conversion_rate": _f(r["conversion_rate"]),
        "revenue": _f(r["revenue"]),
        "avg_days_to_convert": _f(r["avg_days_to_convert"]),
        "avg_opens": _f(r["avg_opens"]),
        "avg_clicks": _f(r["avg_clicks"]),
        "leads_this_year": _i(r["leads_this_year"]),
        "converted_this_year": _i(r["converted_this_year"]),
        "open_rate_this_year": _f(r["open_rate_this_year"]),
        "open_rate_prior": _f(r["open_rate_prior"]),
        "click_rate_this_year": _f(r["click_rate_this_year"]),
        "click_rate_prior": _f(r["click_rate_prior"]),
    }


def _read_conversion_trend(bq):
    """conversion_trend -> data.conversion_trend[] (the 'why did conversion drop' headline series)."""
    sql = f"""
        SELECT cohort_month, leads, converted, conversion_rate, converted_90d,
               conversion_rate_90d, open_rate, click_rate, avg_emails_sent, mature
        FROM `{PROJECT}.{DATASET}.conversion_trend`
        ORDER BY cohort_month
    """
    out = []
    for r in bq.query(sql, location=LOC).result():
        out.append({
            "cohort_month": _date(r["cohort_month"]),
            "leads": _i(r["leads"]),
            "converted": _i(r["converted"]),
            "conversion_rate": _f(r["conversion_rate"]),
            "converted_90d": _i(r["converted_90d"]),
            "conversion_rate_90d": _f(r["conversion_rate_90d"]),
            "open_rate": _f(r["open_rate"]),
            "click_rate": _f(r["click_rate"]),
            "avg_emails_sent": _f(r["avg_emails_sent"]),
            "mature": bool(r["mature"]),
        })
    return out


def _read_monthly(bq):
    sql = f"""
        SELECT month, emails_sent, opens, clicks, open_rate, click_rate, active_leads,
               converted_open_rate, nonconverted_open_rate
        FROM `{PROJECT}.{DATASET}.engagement_monthly`
        ORDER BY month
    """
    out = []
    for r in bq.query(sql, location=LOC).result():
        out.append({
            "month": _date(r["month"]),
            "emails_sent": _i(r["emails_sent"]),
            "opens": _i(r["opens"]),
            "clicks": _i(r["clicks"]),
            "open_rate": _f(r["open_rate"]),
            "click_rate": _f(r["click_rate"]),
            "active_leads": _i(r["active_leads"]),
            "converted_open_rate": _f(r["converted_open_rate"]),
            "nonconverted_open_rate": _f(r["nonconverted_open_rate"]),
        })
    return out


def _read_cohorts(bq):
    """cohort_performance -> data.cohorts[]. Engagement is OPEN-based now (first-email / first-5
    open rate + cumulative % opened); scoped to the complete-data window (submitted >= 2024-08-01)."""
    sql = f"""
        SELECT cohort, leads, converted, conversion_rate, pct_leads_opened,
               first_email_open_rate, first5_open_rate, revenue
        FROM `{PROJECT}.{DATASET}.cohort_performance`
        ORDER BY cohort
    """
    out = []
    for r in bq.query(sql, location=LOC).result():
        out.append({
            "cohort": r["cohort"],
            "leads": _i(r["leads"]),
            "converted": _i(r["converted"]),
            "conversion_rate": _f(r["conversion_rate"]),
            "pct_leads_opened": _f(r["pct_leads_opened"]),
            "first_email_open_rate": _f(r["first_email_open_rate"]),
            "first5_open_rate": _f(r["first5_open_rate"]),
            "revenue": _f(r["revenue"]),
        })
    return out


def _read_leads(bq):
    """quiz_leads -> data.leads[] with the lead's NAME, most recent first."""
    sql = f"""
        SELECT first_name, email, submitted_at, cohort_year, is_converted, revenue_post_quiz,
               order_count_post_quiz, emails_sent, opens, clicks, open_rate, click_rate,
               days_to_convert, last_open_at
        FROM `{PROJECT}.{DATASET}.quiz_leads`
        ORDER BY submitted_at DESC
        LIMIT {LEADS_LIMIT}
    """
    out = []
    for r in bq.query(sql, location=LOC).result():
        out.append({
            "first_name": r["first_name"],
            "email": r["email"],
            "submitted_at": _date(r["submitted_at"]),
            "cohort_year": _i(r["cohort_year"]),
            "is_converted": bool(r["is_converted"]),
            "revenue_post_quiz": _f(r["revenue_post_quiz"]),
            "order_count_post_quiz": _i(r["order_count_post_quiz"]),
            "emails_sent": _i(r["emails_sent"]),
            "opens": _i(r["opens"]),
            "clicks": _i(r["clicks"]),
            "open_rate": _f(r["open_rate"]),
            "click_rate": _f(r["click_rate"]),
            "days_to_convert": r["days_to_convert"] if r["days_to_convert"] is not None else None,
            "last_open_at": _date(r["last_open_at"]),
        })
    return out


def _read_activity(bq):
    """activity_monthly -> data.activity_monthly[] (leads / sales / click_rate per month for the
    combined trend chart, ALL scoped to the quiz-lead cohort). click_rate/clicks/sends stay None
    for months with no email data yet so the dashboard skips them instead of plotting a misleading
    zero. click_rate = emails clicked (binary per send) / emails sent to quiz leads."""
    sql = f"""
        SELECT month, leads, sales, sends, clicks, click_rate
        FROM `{PROJECT}.{DATASET}.activity_monthly`
        ORDER BY month
    """
    out = []
    for r in bq.query(sql, location=LOC).result():
        out.append({
            "month": _date(r["month"]),
            "leads": _i(r["leads"]),
            "sales": _i(r["sales"]),
            "sends": None if r["sends"] is None else int(r["sends"]),
            "clicks": None if r["clicks"] is None else int(r["clicks"]),
            "click_rate": _f(r["click_rate"]),
        })
    return out


def _read_activity_weekly(bq):
    """activity_weekly -> data.activity_weekly[] (same shape as activity_monthly but keyed on the
    week-start date, last 52 complete weeks). Feeds the trend chart's Week view."""
    sql = f"""
        SELECT week, leads, sales, sends, clicks, click_rate
        FROM `{PROJECT}.{DATASET}.activity_weekly`
        ORDER BY week
    """
    out = []
    for r in bq.query(sql, location=LOC).result():
        out.append({
            "week": _date(r["week"]),
            "leads": _i(r["leads"]),
            "sales": _i(r["sales"]),
            "sends": None if r["sends"] is None else int(r["sends"]),
            "clicks": None if r["clicks"] is None else int(r["clicks"]),
            "click_rate": _f(r["click_rate"]),
        })
    return out


def _read_campaigns(bq):
    """lead_campaigns -> data.campaigns[] (per subject line sent to leads: Sent Date, Subject,
    Click rate). Capped at 300 rows, most-recent first."""
    sql = f"""
        SELECT subject, campaign, flow, last_sent, sends, clicks, click_rate
        FROM `{PROJECT}.{DATASET}.lead_campaigns`
        ORDER BY last_sent DESC, sends DESC
        LIMIT 300
    """
    out = []
    for r in bq.query(sql, location=LOC).result():
        out.append({
            "subject": r["subject"],
            "campaign": r["campaign"],
            "flow": r["flow"],
            "last_sent": _date(r["last_sent"]),
            "sends": _i(r["sends"]),
            "clicks": _i(r["clicks"]),
            "click_rate": _f(r["click_rate"]),
        })
    return out


def _read_lead_emails(bq):
    """lead_emails -> data.lead_emails: { email: [[sent_date, subject, opened, clicked], ...] }
    (newest first), for the per-lead drill-down. Compact arrays keep the payload small (~16k rows)."""
    sql = f"""
        SELECT email, sent_at, subject, is_open, is_click
        FROM `{PROJECT}.{DATASET}.lead_emails`
        ORDER BY email, sent_at DESC
    """
    out = {}
    for r in bq.query(sql, location=LOC).result():
        out.setdefault(r["email"], []).append([
            _date(r["sent_at"]),
            r["subject"],
            1 if r["is_open"] else 0,
            1 if r["is_click"] else 0,
        ])
    return out


# ---------------------------------------------------------------------------------------
# Paid media (Meta). Reads the paid_media_* views, which read this client's slice of the
# SHARED raw_windsor.perf_meta -- there is NO Windsor API call in this job, and there must
# never be one. That is the whole point of the canonical ingest layer.
# ---------------------------------------------------------------------------------------
def _read_paid_kpis(bq):
    sql = f"""
        SELECT first_day, last_day, spend, impressions, clicks, link_clicks, leads, purchases,
               revenue, ads, campaigns, active_days, ctr, cpc, cpm, cpl, roas,
               spend_30d, leads_30d, cpl_30d, ctr_30d, roas_30d,
               spend_prev30, leads_prev30, cpl_prev30, ctr_prev30, roas_prev30
        FROM `{PROJECT}.{DATASET}.paid_media_kpis`
    """
    rows = list(bq.query(sql, location=LOC).result())
    if not rows:
        # No Meta rows for this client yet -- the dashboard renders its wired empty state
        # rather than a page of zeros, which would read as "the ads got nothing".
        return None
    r = rows[0]
    return {
        "first_day": _date(r["first_day"]), "last_day": _date(r["last_day"]),
        "spend": _f(r["spend"]), "impressions": _i(r["impressions"]), "clicks": _i(r["clicks"]),
        "link_clicks": _i(r["link_clicks"]), "leads": _i(r["leads"]),
        "purchases": _i(r["purchases"]), "revenue": _f(r["revenue"]),
        "ads": _i(r["ads"]), "campaigns": _i(r["campaigns"]), "active_days": _i(r["active_days"]),
        "ctr": _f(r["ctr"]), "cpc": _f(r["cpc"]), "cpm": _f(r["cpm"]),
        "cpl": _f(r["cpl"]), "roas": _f(r["roas"]),
        "spend_30d": _f(r["spend_30d"]), "leads_30d": _i(r["leads_30d"]),
        "cpl_30d": _f(r["cpl_30d"]), "ctr_30d": _f(r["ctr_30d"]), "roas_30d": _f(r["roas_30d"]),
        "spend_prev30": _f(r["spend_prev30"]), "leads_prev30": _i(r["leads_prev30"]),
        "cpl_prev30": _f(r["cpl_prev30"]), "ctr_prev30": _f(r["ctr_prev30"]),
        "roas_prev30": _f(r["roas_prev30"]),
    }


def _read_paid_daily(bq):
    sql = f"""
        SELECT day, spend, impressions, clicks, link_clicks, leads, landing_page_views,
               add_to_cart, purchases, revenue, ctr, cpc, cpm, cpl, roas, frequency
        FROM `{PROJECT}.{DATASET}.paid_media_daily`
        ORDER BY day
    """
    out = []
    for r in bq.query(sql, location=LOC).result():
        out.append({
            "day": _date(r["day"]), "spend": _f(r["spend"]),
            "impressions": _i(r["impressions"]), "clicks": _i(r["clicks"]),
            "link_clicks": _i(r["link_clicks"]), "leads": _i(r["leads"]),
            "landing_page_views": _i(r["landing_page_views"]),
            "add_to_cart": _i(r["add_to_cart"]), "purchases": _i(r["purchases"]),
            "revenue": _f(r["revenue"]), "ctr": _f(r["ctr"]), "cpc": _f(r["cpc"]),
            "cpm": _f(r["cpm"]), "cpl": _f(r["cpl"]), "roas": _f(r["roas"]),
            "frequency": _f(r["frequency"]),
        })
    return out


def _read_paid_ads(bq):
    sql = f"""
        SELECT ad_id, ad_name, campaign_name, adset_name, thumbnail_url, creative_title,
               destination_url, status, first_day, last_day, spend, impressions, clicks,
               link_clicks, leads, purchases, revenue, ctr, cpc, cpm, cpl, roas, is_significant
        FROM `{PROJECT}.{DATASET}.paid_media_ads`
        ORDER BY spend DESC
        LIMIT {ADS_LIMIT}
    """
    out = []
    for r in bq.query(sql, location=LOC).result():
        out.append({
            "ad_id": r["ad_id"], "ad_name": r["ad_name"], "campaign_name": r["campaign_name"],
            "adset_name": r["adset_name"], "thumbnail_url": r["thumbnail_url"],
            "creative_title": r["creative_title"], "destination_url": r["destination_url"],
            "status": r["status"], "first_day": _date(r["first_day"]),
            "last_day": _date(r["last_day"]), "spend": _f(r["spend"]),
            "impressions": _i(r["impressions"]), "clicks": _i(r["clicks"]),
            "link_clicks": _i(r["link_clicks"]), "leads": _i(r["leads"]),
            "purchases": _i(r["purchases"]), "revenue": _f(r["revenue"]),
            "ctr": _f(r["ctr"]), "cpc": _f(r["cpc"]), "cpm": _f(r["cpm"]),
            "cpl": _f(r["cpl"]), "roas": _f(r["roas"]),
            "is_significant": bool(r["is_significant"]),
        })
    return out


def _read_paid_campaigns(bq):
    sql = f"""
        SELECT campaign_id, campaign_name, objective, ads, first_day, last_day, active_days,
               spend, impressions, clicks, link_clicks, leads, landing_page_views, purchases,
               revenue, ctr, cpc, cpm, cpl, roas
        FROM `{PROJECT}.{DATASET}.paid_media_campaigns`
        ORDER BY spend DESC
    """
    out = []
    for r in bq.query(sql, location=LOC).result():
        out.append({
            "campaign_id": r["campaign_id"], "campaign_name": r["campaign_name"],
            "objective": r["objective"], "ads": _i(r["ads"]),
            "first_day": _date(r["first_day"]), "last_day": _date(r["last_day"]),
            "active_days": _i(r["active_days"]), "spend": _f(r["spend"]),
            "impressions": _i(r["impressions"]), "clicks": _i(r["clicks"]),
            "link_clicks": _i(r["link_clicks"]), "leads": _i(r["leads"]),
            "landing_page_views": _i(r["landing_page_views"]), "purchases": _i(r["purchases"]),
            "revenue": _f(r["revenue"]), "ctr": _f(r["ctr"]), "cpc": _f(r["cpc"]),
            "cpm": _f(r["cpm"]), "cpl": _f(r["cpl"]), "roas": _f(r["roas"]),
        })
    return out


def _read_paid_funnel(bq):
    """Spend beside the quiz funnel it buys. Every ratio here is BLENDED, not attributed --
    there is no click-id join between a Meta ad and a quiz submission. The key names say so
    and the dashboard labels them, so nobody reads them as Meta's own cost per lead."""
    sql = f"""
        SELECT month, spend, impressions, link_clicks, meta_reported_leads, spend_days,
               is_complete_month, quiz_leads, customers, quiz_revenue, avg_days_to_convert,
               quiz_conversion_rate, blended_cost_per_quiz_lead, blended_cost_per_sale,
               blended_return_on_spend
        FROM `{PROJECT}.{DATASET}.paid_media_funnel`
        ORDER BY month
    """
    out = []
    for r in bq.query(sql, location=LOC).result():
        out.append({
            "month": _date(r["month"]), "spend": _f(r["spend"]),
            "impressions": _i(r["impressions"]), "link_clicks": _i(r["link_clicks"]),
            "meta_reported_leads": _i(r["meta_reported_leads"]),
            "spend_days": _i(r["spend_days"]),
            "is_complete_month": bool(r["is_complete_month"]),
            "quiz_leads": _i(r["quiz_leads"]), "customers": _i(r["customers"]),
            "quiz_revenue": _f(r["quiz_revenue"]),
            "avg_days_to_convert": _f(r["avg_days_to_convert"]),
            "quiz_conversion_rate": _f(r["quiz_conversion_rate"]),
            "blended_cost_per_quiz_lead": _f(r["blended_cost_per_quiz_lead"]),
            "blended_cost_per_sale": _f(r["blended_cost_per_sale"]),
            "blended_return_on_spend": _f(r["blended_return_on_spend"]),
        })
    return out


def _data_through(observed, monthly):
    stamps = [ts for ts in (observed or {}).values() if ts]
    if stamps:
        return max(stamps)
    if monthly:
        return monthly[-1]["month"]
    return None


def main():
    bq = bigquery.Client(project=PROJECT)
    gcs = storage.Client(project=PROJECT)
    bucket = gcs.bucket(BUCKET)

    observed = freshness.probe_bq_last_modified(bq, GATING_TABLES, LOC)
    watermark = freshness.read_watermark(bucket, WATERMARK_OBJECT)
    forced = os.environ.get("FORCE_REBUILD") == "1"

    if not forced and not freshness.is_stale(observed, watermark):
        print(f"[{CLIENT}] upstream unchanged since watermark -- fresh, no rebuild.")
        return

    print(f"[{CLIENT}] rebuilding (forced={forced}); reading views from {DATASET}.")
    monthly = _read_monthly(bq)
    data = {
        "client": CLIENT,
        "last_updated": _iso_now(),
        "data_through": _data_through(observed, monthly),
        "kpis": _read_kpis(bq),
        "conversion_trend": _read_conversion_trend(bq),
        "monthly": monthly,
        "activity_monthly": _read_activity(bq),
        "activity_weekly": _read_activity_weekly(bq),
        "campaigns": _read_campaigns(bq),
        "lead_emails": _read_lead_emails(bq),
        "cohorts": _read_cohorts(bq),
        "leads": _read_leads(bq),
        # Paid media (Meta), via the shared raw_windsor.perf_meta -> paid_media_* views.
        # `paid.kpis` is None when this client has no Meta rows yet; the dashboard reads that
        # as "not wired" and renders its empty state instead of a page of honest-looking zeros.
        "paid": {
            "kpis": _read_paid_kpis(bq),
            "daily": _read_paid_daily(bq),
            "ads": _read_paid_ads(bq),
            "campaigns": _read_paid_campaigns(bq),
            "funnel": _read_paid_funnel(bq),
        },
    }

    blob = bucket.blob(DATA_OBJECT)
    blob.cache_control = "no-store"
    blob.upload_from_string(json.dumps(data, separators=(",", ":")), content_type="application/json")
    paid = data["paid"]
    paid_note = "no Meta rows"
    if paid["kpis"]:
        paid_note = "${:,.0f} spend / {} leads".format(
            paid["kpis"]["spend"] or 0, paid["kpis"]["leads"] or 0)
    print(f"[{CLIENT}] uploaded gs://{BUCKET}/{DATA_OBJECT} "
          f"({len(data['conversion_trend'])} cohort-months, {len(monthly)} months, "
          f"{len(data['leads'])} leads; paid: {paid_note}, {len(paid['daily'])} days, "
          f"{len(paid['ads'])} ads, {len(paid['campaigns'])} campaigns).")

    freshness.write_watermark(bucket, WATERMARK_OBJECT, observed)
    print(f"[{CLIENT}] watermark advanced -> {WATERMARK_OBJECT}.")


if __name__ == "__main__":
    main()
