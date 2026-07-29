-- 02_stg_orders.sql -> view stg_orders
--
-- Typed/filtered Shopify orders from the direct-API mirror raw_windsor.tcs_shopify_orders.
-- Keyed on the buyer email (contact_email, falling back to customer_email) -- the join key
-- the quiz-conversion attribution uses. Line-item titles are flattened to a display string,
-- and the marketing-attribution block (source / landing page / UTM / channel) is carried
-- through for the acquisition models.
--
-- DE-DUPE, VERSION-AWARE: the loader appends incrementally AND re-pulls months written by an
-- older loader version, so the same order id legitimately appears more than once: from
-- pull_version 1 (money columns only -- every attribution column NULL) and from v2 (complete).
-- Rank by pull_version FIRST so the complete row wins, then by updated_at so that within a
-- version the freshest edit (refund, fulfilment, tag change) is kept. Ranking by updated_at
-- alone -- as this view used to -- would happily keep a v1 row and blank out the attribution.
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.stg_orders` AS
SELECT
  email, order_name, order_date, subtotal_price, total_price, primary_discount_code, products,
  first_visit_source, first_visit_source_type, first_visit_landing_page, first_visit_referrer_url,
  utm_source, utm_medium, utm_campaign, sales_channel, days_to_convert,
  customer_city, customer_province, customer_country,
  financial_status, cancelled_at, total_refunded, total_shipping, total_tax,
  pull_version
FROM (
  SELECT
    LOWER(TRIM(COALESCE(contact_email, customer_email))) AS email,
    name                            AS order_name,
    created_at                      AS order_date,
    CAST(subtotal_price AS FLOAT64) AS subtotal_price,
    CAST(total_price    AS FLOAT64) AS total_price,
    primary_discount_code,
    ARRAY_TO_STRING(
      ARRAY(SELECT li.title FROM UNNEST(line_items) AS li WHERE li.title IS NOT NULL),
      ', '
    )                               AS products,
    first_visit_source, first_visit_source_type, first_visit_landing_page, first_visit_referrer_url,
    utm_source, utm_medium, utm_campaign, sales_channel, days_to_convert,
    customer_city, customer_province, customer_country,
    financial_status, cancelled_at,
    CAST(total_refunded AS FLOAT64) AS total_refunded,
    CAST(total_shipping AS FLOAT64) AS total_shipping,
    CAST(total_tax      AS FLOAT64) AS total_tax,
    COALESCE(pull_version, 1)       AS pull_version,
    ROW_NUMBER() OVER (
      PARTITION BY id
      ORDER BY COALESCE(pull_version, 1) DESC, updated_at DESC
    ) AS _rn
  FROM `agora-data-driven.raw_windsor.tcs_shopify_orders`
  WHERE COALESCE(contact_email, customer_email) IS NOT NULL
)
WHERE _rn = 1;
