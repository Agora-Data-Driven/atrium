-- 19_paid_media_funnel.sql -> view paid_media_funnel
--
-- Meta SPEND placed beside the quiz funnel it is buying: spend -> quiz leads -> customers ->
-- revenue, by month. This is the join TCS is uniquely able to make, because it already has
-- is_converted / revenue_post_quiz / days_to_convert from the quiz + Shopify models.
--
-- 🔴 THIS IS BLENDED, NOT ATTRIBUTED -- and every column name says so.
--    There is no click-id, UTM or pixel join between a Meta ad and a quiz submission in this
--    warehouse. So `blended_cost_per_quiz_lead` is ALL Meta spend that month divided by ALL
--    quiz leads that month, including leads that arrived from email, organic or direct.
--    It is a real and defensible agency number -- what we paid, against what the business got
--    -- but presenting it as Meta's cost per lead would be a lie, and a client acting on it
--    would over-credit the channel. The dashboard labels every one of these "blended".
--    Meta's OWN in-platform lead count is `meta_reported_leads` here, kept alongside so the
--    two can be compared rather than confused.
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.paid_media_funnel` AS
WITH spend AS (
  SELECT DATE_TRUNC(metric_date, MONTH) AS m,
         SUM(cost)                        AS spend,
         SUM(leads)                       AS meta_reported_leads,
         SUM(impressions)                 AS impressions,
         SUM(link_clicks)                 AS link_clicks,
         -- How much of the month we actually ran ads for. See is_complete_month below.
         COUNT(DISTINCT metric_date)      AS spend_days
  FROM `agora-data-driven.raw_windsor.perf_meta`
  WHERE client_slug = 'tcs'
    AND objective LIKE '%LEAD%'   -- lead-gen campaigns only; see 15_paid_media_daily.sql
  GROUP BY m
),
-- quiz_leads is one row per lead (first submission), already carrying conversion + revenue.
quiz AS (
  SELECT DATE_TRUNC(DATE(submitted_at), MONTH) AS m,
         COUNT(*)                              AS quiz_leads,
         COUNTIF(is_converted)                 AS customers,
         SUM(IFNULL(revenue_post_quiz, 0))     AS quiz_revenue,
         AVG(days_to_convert)                  AS avg_days_to_convert
  FROM `agora-data-driven.client_tcs.quiz_leads`
  GROUP BY m
),
spine AS (
  SELECT m FROM spend UNION DISTINCT SELECT m FROM quiz
)
SELECT
  sp.m                                              AS month,
  ROUND(IFNULL(s.spend, 0), 2)                      AS spend,
  s.impressions                                     AS impressions,
  s.link_clicks                                     AS link_clicks,
  s.meta_reported_leads                             AS meta_reported_leads,
  s.spend_days                                      AS spend_days,
  -- 🔴 A PARTIAL FLIGHT MONTH MAKES EVERY BLENDED RATIO A LIE. TCS's first month ran ads on
  -- 2 days (spend started the 30th) but is divided by a WHOLE month of quiz leads, printing a
  -- $2.48 blended cost per lead against a real ~$20-40. The ratio is only meaningful when the
  -- ads ran for most of the month, so the dashboard charts complete months and greys the rest.
  (s.spend_days >= 0.5 * DATE_DIFF(DATE_ADD(sp.m, INTERVAL 1 MONTH), sp.m, DAY))
                                                    AS is_complete_month,
  IFNULL(q.quiz_leads, 0)                           AS quiz_leads,
  IFNULL(q.customers, 0)                            AS customers,
  ROUND(IFNULL(q.quiz_revenue, 0), 2)               AS quiz_revenue,
  q.avg_days_to_convert                             AS avg_days_to_convert,
  SAFE_DIVIDE(q.customers, q.quiz_leads)            AS quiz_conversion_rate,
  -- The three blended numbers. NULL (not 0) in a month with no spend, so the dashboard
  -- draws a gap rather than a misleading floor.
  SAFE_DIVIDE(s.spend, q.quiz_leads)                AS blended_cost_per_quiz_lead,
  SAFE_DIVIDE(s.spend, q.customers)                 AS blended_cost_per_sale,
  SAFE_DIVIDE(q.quiz_revenue, s.spend)              AS blended_return_on_spend
FROM spine sp
LEFT JOIN spend s ON s.m = sp.m
LEFT JOIN quiz  q ON q.m = sp.m
-- Only months where we actually ran ads: a month of quiz leads with no Meta spend has no
-- paid-media story to tell, and would print an empty row on the paid tab.
WHERE s.spend IS NOT NULL AND s.spend > 0
  AND sp.m < DATE_TRUNC(CURRENT_DATE(), MONTH)   -- drop the in-progress (partial) month
ORDER BY month;
