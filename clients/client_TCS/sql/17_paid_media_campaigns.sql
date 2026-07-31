-- 17_paid_media_campaigns.sql -> view paid_media_campaigns
--
-- One row per CAMPAIGN over its whole flight. Same isolation rule: `WHERE client_slug = 'tcs'`.
-- Campaigns are the level the client actually talks about ("what is the lead-gen campaign
-- costing us?"), so the objective is carried through -- a LEADS campaign and a SALES campaign
-- must never be compared on the same number.
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.paid_media_campaigns` AS
WITH agg AS (
  SELECT
    campaign_id,
    ANY_VALUE(campaign_name)                    AS campaign_name,
    ARRAY_AGG(objective ORDER BY metric_date DESC LIMIT 1)[SAFE_OFFSET(0)] AS objective,
    COUNT(DISTINCT ad_id)                       AS ads,
    MIN(metric_date)                            AS first_day,
    MAX(metric_date)                            AS last_day,
    COUNT(DISTINCT metric_date)                 AS active_days,
    SUM(cost)                                   AS spend,
    SUM(impressions)                            AS impressions,
    SUM(clicks)                                 AS clicks,
    SUM(link_clicks)                            AS link_clicks,
    SUM(leads)                                  AS leads,
    SUM(landing_page_views)                     AS landing_page_views,
    SUM(purchases)                              AS purchases,
    SUM(purchase_value)                         AS revenue
  FROM `agora-data-driven.raw_windsor.perf_meta`
  WHERE client_slug = 'tcs'
    AND objective LIKE '%LEAD%'   -- lead-gen campaigns only; see 15_paid_media_daily.sql
  GROUP BY campaign_id
)
SELECT
  campaign_id, campaign_name, objective, ads, first_day, last_day, active_days,
  ROUND(spend, 2)                          AS spend,
  impressions, clicks, link_clicks, leads, landing_page_views, purchases,
  ROUND(revenue, 2)                        AS revenue,
  SAFE_DIVIDE(link_clicks, impressions)    AS ctr,
  SAFE_DIVIDE(spend, link_clicks)          AS cpc,
  SAFE_DIVIDE(spend, impressions) * 1000   AS cpm,
  SAFE_DIVIDE(spend, leads)                AS cpl,
  SAFE_DIVIDE(revenue, spend)              AS roas
FROM agg
WHERE impressions > 0
ORDER BY spend DESC;
