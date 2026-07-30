-- 16_paid_media_ads.sql -> view paid_media_ads
--
-- One row per AD over its whole flight -- the creative table ("which ads actually work").
-- Same isolation rule as 15_: `WHERE client_slug = 'tcs'` is load-bearing.
--
-- Descriptive fields (ad_name, campaign_name, the thumbnail) are current-state config, not
-- metrics, so they are taken with ANY_VALUE rather than grouped on -- an ad renamed mid-flight
-- must not split into two rows and halve its own numbers.
--
-- ⚠️ A VOLUME FLOOR is applied downstream, not here: an ad with 4 clicks can post a $0.18 CPC
-- and win "best creative" on noise. The view exposes the raw counts and lets the job/dashboard
-- decide; `is_significant` marks the rows that carry enough volume to be ranked on a rate.
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.paid_media_ads` AS
WITH agg AS (
  SELECT
    ad_id,
    ANY_VALUE(ad_name)                                AS ad_name,
    ANY_VALUE(campaign_name)                          AS campaign_name,
    ANY_VALUE(adset_name)                             AS adset_name,
    -- The three thumbnail columns populate inconsistently per placement; take the first
    -- non-null so an ad with only an Instagram creative still shows a picture.
    ANY_VALUE(COALESCE(creative_thumbnail_url, ig_thumbnail_url, placement_thumbnail_url))
                                                      AS thumbnail_url,
    ANY_VALUE(creative_title)                         AS creative_title,
    ANY_VALUE(creative_body)                          AS creative_body,
    ANY_VALUE(destination_url)                        AS destination_url,
    -- effective_status is CURRENT state, so the most recent row's value is the true one.
    ARRAY_AGG(effective_status ORDER BY metric_date DESC LIMIT 1)[SAFE_OFFSET(0)] AS status,
    MIN(metric_date)                                  AS first_day,
    MAX(metric_date)                                  AS last_day,
    SUM(cost)                                         AS spend,
    SUM(impressions)                                  AS impressions,
    SUM(clicks)                                       AS clicks,
    SUM(link_clicks)                                  AS link_clicks,
    SUM(leads)                                        AS leads,
    SUM(purchases)                                    AS purchases,
    SUM(purchase_value)                               AS revenue,
    SUM(video_starts)                                 AS video_starts,
    SUM(thruplays)                                    AS thruplays
  FROM `agora-data-driven.raw_windsor.perf_meta`
  WHERE client_slug = 'tcs'
  GROUP BY ad_id
)
SELECT
  ad_id, ad_name, campaign_name, adset_name, thumbnail_url,
  creative_title, creative_body, destination_url, status, first_day, last_day,
  ROUND(spend, 2)                            AS spend,
  impressions, clicks, link_clicks, leads, purchases,
  ROUND(revenue, 2)                          AS revenue,
  video_starts, thruplays,
  SAFE_DIVIDE(link_clicks, impressions)      AS ctr,
  SAFE_DIVIDE(spend, link_clicks)            AS cpc,
  SAFE_DIVIDE(spend, impressions) * 1000     AS cpm,
  SAFE_DIVIDE(spend, leads)                  AS cpl,
  SAFE_DIVIDE(revenue, spend)                AS roas,
  -- Enough volume to be judged on a RATE. Below this, read the counts only.
  (link_clicks >= 30 OR leads >= 5)          AS is_significant
FROM agg
WHERE impressions > 0
ORDER BY spend DESC;
