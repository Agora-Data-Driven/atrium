r"""Create the raw_windsor.perf_meta table (idempotent).

GRAIN: ONE ROW per (ad_id x metric_date) -- the finest level Meta exposes without
breakdown dimensions, so you can roll up to adset / campaign / account in SQL and join
creative metadata for creative-level views. (ad_id, metric_date) is also the MERGE key
meta_loader.py writes on.

NO breakdown dimensions (publisher_platform, age, gender, region, device...) are stored
here -- any of those would multiply rows and change the grain. Demographic/placement
splits belong in a separate table.

PORTED from bidbrain-analytics/ingest/windsor_data_pull/meta/create_meta_table.py. The
table name and column vocabulary are kept IDENTICAL to bidbrain on purpose, so the two
estates do not diverge and the loader stays close to a copy. Deliberate differences:

  * project/dataset -> agora-data-driven.raw_windsor (location asia-southeast1)
  * bidbrain's two `signup_button_*` columns are DROPPED -- they are a custom pixel
    conversion belonging to one bidbrain account (resetdata) and return nothing here.
  * `datasource` is stored explicitly. Windsor's /all endpoint is BLENDED across
    connectors (verified live 2026-07-30: `ASL Logistics` comes back as google_ads and
    carries NO ad_id), so the loader filters to datasource == "facebook" and this
    column is what lets an auditor prove it.
  * `raw_row_count` records how many raw Windsor rows were summed into this row -- see
    the collision note in meta_loader.py. Normally 1.

Run:  .\.venv\Scripts\python.exe services\ingest\meta\create_meta_table.py
      (after services\ingest\create_dataset.py)

Idempotent (exists_ok=True) -- but note it CREATES, it does not ALTER. If the table
already exists from an earlier, narrower version, drop it first so the new columns take
effect (it has never held data, so this is free):

    bq rm -f -t agora-data-driven:raw_windsor.perf_meta

Auth: Application Default Credentials (ADC).
"""

import os

from google.cloud import bigquery

# Single region for everything in this project (Singapore). The dataset's location is
# immutable, and the table inherits it -- this constant is load-bearing.
LOCATION = "asia-southeast1"
PROJECT = os.environ.get("GCP_PROJECT", "agora-data-driven")
RAW_DATASET = os.environ.get("RAW_DATASET", "raw_windsor")
TABLE = "perf_meta"

SCHEMA = [
    # ---- Identifiers / dimensions ----
    bigquery.SchemaField("platform", "STRING", mode="REQUIRED",
                         description="Source platform, always 'meta'"),
    bigquery.SchemaField("account_id", "STRING",
                         description="Meta ad-account id; the ACCOUNT_TO_CLIENT key"),
    bigquery.SchemaField("account_name", "STRING"),
    bigquery.SchemaField("campaign_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("campaign_name", "STRING", description="Windsor 'campaign'"),
    bigquery.SchemaField("objective", "STRING",
                         description="Objective, e.g. OUTCOME_LEADS / OUTCOME_SALES"),
    bigquery.SchemaField("adset_id", "STRING"),
    bigquery.SchemaField("adset_name", "STRING"),
    bigquery.SchemaField("ad_id", "STRING", mode="REQUIRED",
                         description="Finest grain; part of the MERGE key"),
    bigquery.SchemaField("ad_name", "STRING"),
    bigquery.SchemaField("effective_status", "STRING",
                         description="Effective delivery status (ACTIVE, PAUSED, ...)"),
    bigquery.SchemaField("client_slug", "STRING",
                         description="Internal client key. THE view filter: a per-client view is "
                                     "WHERE client_slug = '<c>'. NULL means the account is not "
                                     "mapped to any client -- deliberately, so unmapped spend can "
                                     "never default into somebody else's dashboard. "
                                     "S7000 splits into TWO slugs (into, service7000)."),
    bigquery.SchemaField("agency_slug", "STRING",
                         description="Always 'agora' for mapped accounts; NULL when unmapped. "
                                     "Kept for schema parity with bidbrain.perf_meta."),
    bigquery.SchemaField("metric_date", "DATE", mode="REQUIRED",
                         description="Date of metrics; part of the MERGE key"),
    bigquery.SchemaField("currency", "STRING", description="Windsor account_currency"),
    bigquery.SchemaField("campaign_spend_cap", "NUMERIC",
                         description="Campaign spend cap (current-state config, not a metric)"),

    # ---- Delivery & cost ----
    bigquery.SchemaField("impressions", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("reach", "INT64",
                         description="Unique people reached. NOT additive across days -- never "
                                     "SUM() this over a date range; use impressions/frequency."),
    bigquery.SchemaField("frequency", "FLOAT64",
                         description="impressions / reach. A RATE: aggregate it "
                                     "impression-weighted, never AVG()."),
    bigquery.SchemaField("cost", "NUMERIC", mode="REQUIRED",
                         description="Windsor 'spend', already in account currency (not micros)"),
    bigquery.SchemaField("cpc", "NUMERIC", description="Meta-computed cost per click (all)"),
    bigquery.SchemaField("cpm", "NUMERIC", description="Meta-computed cost per 1k impressions"),
    bigquery.SchemaField("cpp", "NUMERIC", description="Meta-computed cost per 1k reached"),

    # ---- Clicks: all ----
    bigquery.SchemaField("clicks", "INT64", mode="REQUIRED",
                         description="All clicks (incl. reactions, comments, expands)"),
    bigquery.SchemaField("unique_clicks", "INT64", description="Not additive across days"),

    # ---- Clicks: link (use these for CTR / CPC) ----
    bigquery.SchemaField("link_clicks", "INT64"),
    bigquery.SchemaField("link_clicks_actions", "INT64",
                         description="actions_link_click (alt path; usually == link_clicks)"),
    bigquery.SchemaField("unique_link_clicks", "INT64",
                         description="unique_actions_link_click. One of the TWO fields that cap "
                                     "the fetch window at 13 months -- see meta_loader.py."),
    bigquery.SchemaField("unique_link_clicks_ctr", "FLOAT64",
                         description="Windsor PERCENT scale -- verify 0-1 vs 0-100 on first load"),
    bigquery.SchemaField("cost_per_link_click", "NUMERIC"),
    bigquery.SchemaField("cost_per_unique_link_click", "NUMERIC"),

    # ---- Clicks: outbound ----
    bigquery.SchemaField("outbound_clicks", "INT64"),
    bigquery.SchemaField("unique_outbound_clicks", "INT64"),
    bigquery.SchemaField("outbound_ctr", "FLOAT64", description="Windsor PERCENT scale"),
    bigquery.SchemaField("unique_outbound_ctr", "FLOAT64", description="Windsor PERCENT scale"),
    bigquery.SchemaField("cost_per_outbound_click", "NUMERIC"),
    bigquery.SchemaField("cost_per_unique_outbound_click", "NUMERIC"),

    # ---- Engagement ----
    bigquery.SchemaField("post_engagement", "INT64"),
    bigquery.SchemaField("unique_post_engagement", "INT64"),
    bigquery.SchemaField("page_engagement", "INT64"),
    bigquery.SchemaField("reactions", "INT64"),
    bigquery.SchemaField("comments", "INT64"),
    bigquery.SchemaField("shares", "INT64", description="Windsor actions_post"),
    bigquery.SchemaField("saves", "INT64"),
    bigquery.SchemaField("video_3s_views", "INT64", description="3-second video plays"),

    # ---- Awareness / brand ----
    bigquery.SchemaField("est_ad_recall_lift", "NUMERIC",
                         description="estimated_ad_recallers; only on awareness objectives"),
    bigquery.SchemaField("est_ad_recall_rate", "FLOAT64",
                         description="estimated_ad_recall_rate (Windsor PERCENT scale)"),
    bigquery.SchemaField("instagram_profile_visits", "INT64"),

    # ---- Leads ----
    # Verified live 2026-07-30 over last_90d: `leads` is the estate-wide total, and the two
    # sub-fields split cleanly by client -- MeloYelo/riverdance leads are 100% pixel
    # (leads_website), rhe's are 100% on-Facebook lead forms (leads_onfacebook), and TCS
    # genuinely mixes both (83 pixel + 25 on-Facebook = 108). Read `leads`, not a sub-field,
    # unless you specifically want one path.
    bigquery.SchemaField("leads", "INT64",
                         description="All leads: FB forms + Messenger + off-FB pixel (actions_lead)"),
    bigquery.SchemaField("leads_website", "INT64", description="Website/pixel leads"),
    bigquery.SchemaField("leads_onfacebook", "INT64", description="On-Facebook lead-form leads"),
    bigquery.SchemaField("unique_leads", "INT64",
                         description="unique_actions_lead. The OTHER 13-month window-capping "
                                     "field. Can be served but identically 0 account-wide -- "
                                     "render that as n/a, never 0."),
    bigquery.SchemaField("cost_per_lead", "NUMERIC", description="cost_per_action_type_lead"),

    # ---- Conversions & value (e-commerce; null/0 for lead-gen clients) ----
    bigquery.SchemaField("landing_page_views", "INT64"),
    bigquery.SchemaField("add_to_cart", "INT64"),
    bigquery.SchemaField("initiate_checkout", "INT64"),
    bigquery.SchemaField("purchases", "INT64",
                         description="actions_omni_purchase. OMNI on purpose: verified live "
                                     "2026-07-30 that Sabbath Spa reports 28 purchases on omni "
                                     "and ZERO on actions_purchase, while every other account "
                                     "agrees across both. Omni is the widest label here."),
    bigquery.SchemaField("purchases_website", "INT64",
                         description="actions_offsite_conversion_fb_pixel_purchase -- the pixel "
                                     "path only. riverdance's own job reads this label."),
    bigquery.SchemaField("registrations", "INT64",
                         description="actions_complete_registration. Served by this account but "
                                     "identically 0 everywhere (verified 2026-07-30) -- treat 0 "
                                     "as 'not measured', never as 'none happened'."),
    bigquery.SchemaField("purchase_value", "NUMERIC", description="action_values_omni_purchase"),
    bigquery.SchemaField("purchase_value_website", "NUMERIC",
                         description="action_values_offsite_conversion_fb_pixel_purchase"),
    bigquery.SchemaField("purchase_roas", "NUMERIC",
                         description="purchase_roas_omni_purchase. A RATIO -- never SUM() it; "
                                     "recompute as purchase_value / cost over any range."),
    bigquery.SchemaField("purchase_roas_website", "NUMERIC",
                         description="website_purchase_roas_offsite_conversion_fb_pixel_purchase"),

    # ---- Video funnel ----
    bigquery.SchemaField("video_starts", "INT64", description="video_play_actions"),
    bigquery.SchemaField("video_25", "INT64"),
    bigquery.SchemaField("video_50", "INT64"),
    bigquery.SchemaField("video_75", "INT64"),
    bigquery.SchemaField("video_95", "INT64"),
    bigquery.SchemaField("video_completes", "INT64", description="watched at 100%"),
    bigquery.SchemaField("thruplays", "INT64",
                         description="Played to completion or >=15s -- Meta's preferred metric"),
    bigquery.SchemaField("video_avg_watch_time", "FLOAT64",
                         description="Average video play time, seconds. A RATE."),

    # ---- Optimization signals ----
    bigquery.SchemaField("quality_ranking", "STRING"),
    bigquery.SchemaField("engagement_rate_ranking", "STRING"),
    bigquery.SchemaField("conversion_rate_ranking", "STRING"),

    # ---- Creative metadata ----
    bigquery.SchemaField("creative_id", "STRING"),
    bigquery.SchemaField("creative_thumbnail_url", "STRING", description="thumbnail_url"),
    bigquery.SchemaField("ig_thumbnail_url", "STRING",
                         description="effective_instagram_media__thumbnail_url"),
    bigquery.SchemaField("placement_thumbnail_url", "STRING",
                         description="placement_ad_thumbnail_url"),
    bigquery.SchemaField("creative_title", "STRING", description="Windsor 'title'"),
    bigquery.SchemaField("creative_body", "STRING", description="Windsor 'body'"),
    bigquery.SchemaField("creative_link_url", "STRING", description="Windsor 'link_url'"),
    bigquery.SchemaField("destination_url", "STRING",
                         description="Windsor 'link' (where the ad clicks through to)"),

    # ---- Provenance ----
    bigquery.SchemaField("datasource", "STRING",
                         description="Windsor 'datasource'. Always 'facebook' -- the /all endpoint "
                                     "is blended across connectors and the loader filters on this."),
    bigquery.SchemaField("ingested_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("source", "STRING", mode="REQUIRED", description="'windsor.facebook'"),
    bigquery.SchemaField("raw_row_count", "INT64",
                         description="How many raw Windsor rows were SUMMED into this row. "
                                     "Normally 1; >1 means Windsor returned rows sharing "
                                     "(ad_id, date) that differed only on a descriptive field."),
    bigquery.SchemaField("raw_row", "JSON",
                         description="Full original (flat) Windsor row, for fidelity. When "
                                     "raw_row_count > 1 this is the merged object and carries "
                                     "_collapsed_from listing the contributing variants."),
]


def main() -> None:
    bq = bigquery.Client(project=PROJECT, location=LOCATION)
    table_id = f"{PROJECT}.{RAW_DATASET}.{TABLE}"

    table = bigquery.Table(table_id, schema=SCHEMA)
    # Partitioned by the date you always filter on; clustered by the two ids every
    # per-client view and every rollup touches. client_slug leads the cluster key because
    # a per-client view is literally WHERE client_slug = '<c>'.
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY,
        field="metric_date",
    )
    table.clustering_fields = ["client_slug", "campaign_id", "ad_id"]
    table.description = (
        "Meta/Facebook Ads performance, one row per (ad x date), via Windsor.ai. "
        "Written by the shared windsor-meta-ingest job; read by per-client views "
        "(WHERE client_slug = '<c>'). A client job must never call Windsor itself."
    )

    # exists_ok=True makes this idempotent (re-running converges, does not error).
    table = bq.create_table(table, exists_ok=True)
    print(f"[OK] table ready: {table_id} (location {LOCATION})")
    print(f"     partitioned by: {table.time_partitioning.field}")
    print(f"     clustered by:   {table.clustering_fields}")
    print(f"     columns:        {len(table.schema)}")


if __name__ == "__main__":
    main()
