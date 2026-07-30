-- 15_paid_media_daily.sql -> view paid_media_daily
--
-- 🔴 THIS VIEW IS THE ISOLATION BOUNDARY. `WHERE client_slug = 'tcs'` is the ONLY thing
-- standing between this client's dashboard and twelve other accounts' spend in the shared
-- raw_windsor.perf_meta table. Never relax it, never replace it with an account_name LIKE.
--
-- The canonical shape: ONE shared loader (services/ingest/meta) writes raw_windsor.perf_meta
-- for the whole estate; each client reads its own slice through a view like this one. TCS is
-- the first client on the canonical path -- it has NO client-side Windsor API call, unlike
-- the four legacy API-LIVE clients.
--
-- One row per DAY. Grain in perf_meta is (ad x day), so everything here is a rollup.
--
-- ⚠️ THREE METRICS THAT CANNOT BE SUMMED, and are not:
--   * reach       -- unique PEOPLE. The same person seen on two days is one reach, not two.
--                    Summing it across days overstates it, so it is deliberately NOT exposed
--                    as a daily total here; use impressions and frequency instead.
--   * frequency   -- a RATE (impressions/reach). Carried IMPRESSION-WEIGHTED, never AVG().
--   * purchase_roas -- a RATIO. Recomputed from summed revenue / summed spend, never summed.
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.paid_media_daily` AS
SELECT
  metric_date                                              AS day,
  ROUND(SUM(cost), 2)                                      AS spend,
  SUM(impressions)                                         AS impressions,
  SUM(clicks)                                              AS clicks,
  SUM(link_clicks)                                         AS link_clicks,
  SUM(leads)                                               AS leads,
  SUM(landing_page_views)                                  AS landing_page_views,
  SUM(add_to_cart)                                         AS add_to_cart,
  SUM(purchases)                                           AS purchases,
  ROUND(SUM(purchase_value), 2)                            AS revenue,
  -- Rates derived from the TOTALS, never averaged across rows.
  SAFE_DIVIDE(SUM(link_clicks), SUM(impressions))          AS ctr,
  SAFE_DIVIDE(SUM(cost), SUM(link_clicks))                 AS cpc,
  SAFE_DIVIDE(SUM(cost), SUM(impressions)) * 1000          AS cpm,
  SAFE_DIVIDE(SUM(cost), SUM(leads))                       AS cpl,
  SAFE_DIVIDE(SUM(purchase_value), SUM(cost))              AS roas,
  -- Impression-weighted, because frequency is a rate and reach is not additive.
  SAFE_DIVIDE(SUM(frequency * impressions), SUM(impressions)) AS frequency
FROM `agora-data-driven.raw_windsor.perf_meta`
WHERE client_slug = 'tcs'
GROUP BY day
ORDER BY day;
