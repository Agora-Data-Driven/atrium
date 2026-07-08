-- 11_activity_monthly.sql -> view activity_monthly
--
-- Month-on-month LEADS (new quiz leads), SALES (orders), and CLICKS (emails clicked) for the
-- combined trend chart with a relative/absolute toggle. Kept to the last 24 months so the three
-- series compare on the same recent window.
--
-- CLICKS is left NULL for a month with NO email data loaded (LEFT JOIN miss) so the chart skips
-- it rather than implying zero while the Klaviyo backfill is still catching up; a month that DID
-- have sends but zero clicks correctly reads 0. LEADS/SALES are counts (0 when none that month).
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.activity_monthly` AS
WITH leads AS (
  SELECT DATE_TRUNC(DATE(submitted_at), MONTH) AS m, COUNT(*) AS n
  FROM `agora-data-driven.client_tcs.stg_quiz` GROUP BY 1
),
sales AS (
  SELECT DATE_TRUNC(DATE(order_date), MONTH) AS m, COUNT(*) AS n
  FROM `agora-data-driven.client_tcs.stg_orders` GROUP BY 1
),
email AS (
  SELECT DATE_TRUNC(DATE(event_at), MONTH) AS m,
         COUNT(*) AS sends, COUNTIF(is_click) AS clicks
  FROM `agora-data-driven.client_tcs.stg_email_events` GROUP BY 1
),
spine AS (
  SELECT m FROM leads UNION DISTINCT SELECT m FROM sales UNION DISTINCT SELECT m FROM email
)
SELECT
  sp.m               AS month,
  COALESCE(l.n, 0)   AS leads,
  COALESCE(s.n, 0)   AS sales,
  e.sends            AS sends,
  e.clicks           AS clicks
FROM spine sp
LEFT JOIN leads l ON l.m = sp.m
LEFT JOIN sales s ON s.m = sp.m
LEFT JOIN email e ON e.m = sp.m
WHERE sp.m >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 24 MONTH), MONTH)
ORDER BY month;
