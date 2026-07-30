-- 18_paid_media_kpis.sql -> view paid_media_kpis
--
-- ONE row: the paid-media headline tiles, plus a like-for-like LAST 30 DAYS vs PRIOR 30 DAYS
-- comparison so every tile can carry a direction instead of a bare number.
-- Same isolation rule: `WHERE client_slug = 'tcs'`.
--
-- ⚠️ The comparison windows are anchored to the LAST DAY WITH DATA, not to CURRENT_DATE().
-- Windsor lands yesterday's data overnight, so anchoring on today would compare a 29-day
-- window against a full 30-day one and print a fake decline every single morning.
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.paid_media_kpis` AS
WITH src AS (
  SELECT * FROM `agora-data-driven.raw_windsor.perf_meta` WHERE client_slug = 'tcs'
),
bounds AS (
  SELECT MIN(metric_date) AS first_day, MAX(metric_date) AS last_day FROM src
),
win AS (
  SELECT
    b.first_day, b.last_day,
    DATE_SUB(b.last_day, INTERVAL 29 DAY) AS cur_from,
    DATE_SUB(b.last_day, INTERVAL 59 DAY) AS prev_from,
    DATE_SUB(b.last_day, INTERVAL 30 DAY) AS prev_to
  FROM bounds b
),
life AS (
  SELECT
    SUM(cost) AS spend, SUM(impressions) AS impressions, SUM(clicks) AS clicks,
    SUM(link_clicks) AS link_clicks, SUM(leads) AS leads, SUM(purchases) AS purchases,
    SUM(purchase_value) AS revenue, COUNT(DISTINCT ad_id) AS ads,
    COUNT(DISTINCT campaign_id) AS campaigns, COUNT(DISTINCT metric_date) AS active_days
  FROM src
),
cur AS (
  SELECT SUM(s.cost) AS spend, SUM(s.leads) AS leads, SUM(s.link_clicks) AS link_clicks,
         SUM(s.impressions) AS impressions, SUM(s.purchase_value) AS revenue
  FROM src s, win w WHERE s.metric_date BETWEEN w.cur_from AND w.last_day
),
prev AS (
  SELECT SUM(s.cost) AS spend, SUM(s.leads) AS leads, SUM(s.link_clicks) AS link_clicks,
         SUM(s.impressions) AS impressions, SUM(s.purchase_value) AS revenue
  FROM src s, win w WHERE s.metric_date BETWEEN w.prev_from AND w.prev_to
)
SELECT
  w.first_day, w.last_day,
  -- ---- lifetime ----
  ROUND(l.spend, 2)                             AS spend,
  l.impressions, l.clicks, l.link_clicks, l.leads, l.purchases,
  ROUND(l.revenue, 2)                           AS revenue,
  l.ads, l.campaigns, l.active_days,
  SAFE_DIVIDE(l.link_clicks, l.impressions)     AS ctr,
  SAFE_DIVIDE(l.spend, l.link_clicks)           AS cpc,
  SAFE_DIVIDE(l.spend, l.impressions) * 1000    AS cpm,
  SAFE_DIVIDE(l.spend, l.leads)                 AS cpl,
  SAFE_DIVIDE(l.revenue, l.spend)               AS roas,
  -- ---- last 30 days ----
  ROUND(c.spend, 2)                             AS spend_30d,
  c.leads                                       AS leads_30d,
  SAFE_DIVIDE(c.spend, c.leads)                 AS cpl_30d,
  SAFE_DIVIDE(c.link_clicks, c.impressions)     AS ctr_30d,
  SAFE_DIVIDE(c.revenue, c.spend)               AS roas_30d,
  -- ---- prior 30 days (the like-for-like baseline) ----
  ROUND(p.spend, 2)                             AS spend_prev30,
  p.leads                                       AS leads_prev30,
  SAFE_DIVIDE(p.spend, p.leads)                 AS cpl_prev30,
  SAFE_DIVIDE(p.link_clicks, p.impressions)     AS ctr_prev30,
  SAFE_DIVIDE(p.revenue, p.spend)               AS roas_prev30
FROM win w, life l, cur c, prev p;
