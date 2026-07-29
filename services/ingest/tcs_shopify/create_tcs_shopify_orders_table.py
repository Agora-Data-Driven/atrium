"""Create the raw_windsor.tcs_shopify_orders table (idempotent).

TCS's Shopify orders slot in the shared raw layer: order identity, contact/customer email,
dates, money totals, discount codes, line items, the customerJourneySummary MARKETING
ATTRIBUTION block (source / landing page / UTM / channel), the customer dimension, and order
state + net-revenue components. Direct-API source (not Windsor) -- see tcs_shopify_loader.py,
which is the authority on this schema and widens the table itself when it gains columns.
Created in asia-southeast1 alongside the rest of the project.

Auth: Application Default Credentials (ADC).
"""

import os

from google.cloud import bigquery

LOCATION = "asia-southeast1"
PROJECT = os.environ.get("GCP_PROJECT", "agora-data-driven")
RAW_DATASET = os.environ.get("RAW_DATASET", "raw_windsor")
TABLE = "tcs_shopify_orders"

SCHEMA = [
    bigquery.SchemaField("id", "INT64"),
    bigquery.SchemaField("name", "STRING"),
    bigquery.SchemaField("contact_email", "STRING"),
    bigquery.SchemaField("customer_email", "STRING"),
    bigquery.SchemaField("customer_first_name", "STRING"),
    bigquery.SchemaField("customer_last_name", "STRING"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
    bigquery.SchemaField("updated_at", "TIMESTAMP"),
    bigquery.SchemaField("currency", "STRING"),
    bigquery.SchemaField("subtotal_price", "NUMERIC"),
    bigquery.SchemaField("total_discounts", "NUMERIC"),
    bigquery.SchemaField("total_price", "NUMERIC"),
    bigquery.SchemaField("primary_discount_code", "STRING"),
    bigquery.SchemaField("discount_codes", "RECORD", mode="REPEATED", fields=[
        bigquery.SchemaField("code", "STRING"),
    ]),
    bigquery.SchemaField("line_items", "RECORD", mode="REPEATED", fields=[
        bigquery.SchemaField("title", "STRING"),
        bigquery.SchemaField("variant_title", "STRING"),
        bigquery.SchemaField("sku", "STRING"),
        bigquery.SchemaField("quantity", "INT64"),
        bigquery.SchemaField("price", "NUMERIC"),
        bigquery.SchemaField("vendor", "STRING"),
    ]),
    # --- marketing attribution: Shopify customerJourneySummary (pull_version 2) ---------
    bigquery.SchemaField("days_to_convert", "INT64"),
    bigquery.SchemaField("moments_count", "INT64"),
    bigquery.SchemaField("first_visit_source", "STRING"),
    bigquery.SchemaField("first_visit_source_type", "STRING"),
    bigquery.SchemaField("first_visit_landing_page", "STRING"),
    bigquery.SchemaField("first_visit_referrer_url", "STRING"),
    bigquery.SchemaField("utm_source", "STRING"),
    bigquery.SchemaField("utm_medium", "STRING"),
    bigquery.SchemaField("utm_campaign", "STRING"),
    bigquery.SchemaField("utm_term", "STRING"),
    bigquery.SchemaField("last_visit_source", "STRING"),
    bigquery.SchemaField("last_visit_landing_page", "STRING"),
    bigquery.SchemaField("sales_channel", "STRING"),
    # --- customer dimension -------------------------------------------------------------
    bigquery.SchemaField("customer_id", "INT64"),
    bigquery.SchemaField("customer_city", "STRING"),
    bigquery.SchemaField("customer_province", "STRING"),
    bigquery.SchemaField("customer_country", "STRING"),
    bigquery.SchemaField("customer_orders_count", "INT64"),
    bigquery.SchemaField("customer_total_spent", "NUMERIC"),
    # --- order state + net-revenue components -------------------------------------------
    bigquery.SchemaField("financial_status", "STRING"),
    bigquery.SchemaField("fulfillment_status", "STRING"),
    bigquery.SchemaField("cancel_reason", "STRING"),
    bigquery.SchemaField("cancelled_at", "TIMESTAMP"),
    bigquery.SchemaField("tags", "STRING", mode="REPEATED"),
    bigquery.SchemaField("total_refunded", "NUMERIC"),
    bigquery.SchemaField("total_shipping", "NUMERIC"),
    bigquery.SchemaField("total_tax", "NUMERIC"),
    # Which loader version wrote the row. NULL/1 rows predate the attribution block, so their
    # attribution columns are NULL rather than genuinely empty -- the loader re-pulls those
    # months and stg_orders keeps the highest-version row per order id.
    bigquery.SchemaField("pull_version", "INT64"),
]


def main() -> None:
    bq = bigquery.Client(project=PROJECT)
    table_id = f"{PROJECT}.{RAW_DATASET}.{TABLE}"
    table = bigquery.Table(table_id, schema=SCHEMA)
    bq.create_table(table, exists_ok=True)
    print(f"[OK] table ready: {table_id} (location {LOCATION})")


if __name__ == "__main__":
    main()
