-- 20_paid_media_funnel_stages.sql -> view paid_media_funnel_stages
--
-- The reach -> leads funnel, PER AD, as step-through rates. This is what the heatmap on the
-- Lead Gen tab reads: one row per ad, one column per funnel step, so the WEAK STEP is visible
-- at a glance instead of having to be inferred from five separate totals.
--
-- The chain, and what each step actually means:
--
--   reach          people the ad was shown to
--     -> impressions        how often it was shown  (impr/reach = frequency)
--     -> link_clicks        they clicked the link   (ctr)
--     -> landing_page_views the page actually LOADED (lp_rate -- the silent killer: a click
--                           that never becomes a page view is a slow page or a bad redirect,
--                           and it is invisible in every CTR-based report)
--     -> leads              they converted          (lead_rate)
--
-- 🔴 REACH IS NOT ADDITIVE, and this view does NOT pretend otherwise.
--    Meta dedupes reach only within the window you query it for. Our grain is (ad x day), so
--    summing it counts a person reached on three days three times. There is no way to derive a
--    true multi-day unique reach from this table. So the column is named `reach_daily_sum`, the
--    dashboard labels it "daily sum", and `impr_per_reach` is therefore a DAILY frequency.
--    Every rate below is comparable BETWEEN ads (they are all computed the same way), which is
--    what a heatmap is for -- but do not read reach_daily_sum as "people".
--
-- Lead-gen campaigns only, same as every other paid_media_* view (see 15_paid_media_daily.sql).
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.paid_media_funnel_stages` AS
WITH agg AS (
  SELECT
    ad_id,
    ANY_VALUE(ad_name)                  AS ad_name,
    ANY_VALUE(campaign_name)            AS campaign_name,
    SUM(cost)                           AS spend,
    SUM(reach)                          AS reach_daily_sum,
    SUM(impressions)                    AS impressions,
    SUM(link_clicks)                    AS link_clicks,
    SUM(landing_page_views)             AS landing_page_views,
    SUM(leads)                          AS leads
  FROM `agora-data-driven.raw_windsor.perf_meta`
  WHERE client_slug = 'tcs'
    AND objective LIKE '%LEAD%'
  GROUP BY ad_id
)
SELECT
  ad_id, ad_name, campaign_name,
  ROUND(spend, 2)                                        AS spend,
  reach_daily_sum, impressions, link_clicks, landing_page_views, leads,
  -- Step-through rates. SAFE_DIVIDE everywhere: an ad with no reach must render as a gap,
  -- not as a zero (zero reads as "nobody converted", which is a different claim).
  SAFE_DIVIDE(impressions, reach_daily_sum)              AS impr_per_reach,
  SAFE_DIVIDE(link_clicks, impressions)                  AS ctr,
  SAFE_DIVIDE(landing_page_views, link_clicks)           AS lp_rate,
  SAFE_DIVIDE(leads, landing_page_views)                 AS lead_rate,
  -- End to end, and the money question.
  SAFE_DIVIDE(leads, reach_daily_sum)                    AS leads_per_reach,
  SAFE_DIVIDE(spend, leads)                              AS cpl,
  -- Same volume floor as paid_media_ads: a rate on four clicks is noise, and noise wins
  -- heatmaps just as easily as it wins tables.
  (link_clicks >= 30 OR leads >= 5)                      AS is_significant
FROM agg
WHERE impressions > 0
ORDER BY spend DESC;
