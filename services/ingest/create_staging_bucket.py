r"""Create the shared ingest STAGING bucket (idempotent).

Every Windsor connector loader stages its rows as NDJSON in GCS before the BigQuery load
job, then MERGEs the staging table into the main table. That needs one private bucket,
shared by every loader -- the sibling of create_dataset.py, which creates the shared dataset.

The name matches the STAGING_BUCKET value that tools/deploy_ingest_jobs.ps1 already sets on
every ingest job, and that script already grants ingest-runner@ roles/storage.objectAdmin on
it -- so this script only has to make the bucket exist.

🔴 PRIVATE, and it stays private. These objects are raw client performance data. Uniform
bucket-level access is enforced so no object can acquire its own public ACL.

Objects are throwaway: the loader deletes its local NDJSON after a successful load, and a
lifecycle rule deletes the GCS copies after LIFECYCLE_DAYS so the bucket cannot grow forever.
A failed load leaves its object behind on purpose -- it is the evidence for a re-run.

Run:  .\.venv\Scripts\python.exe services\ingest\create_staging_bucket.py

Auth: Application Default Credentials (ADC).
"""

import os

from google.cloud import storage

# Single region for everything in this project (Singapore).
LOCATION = "asia-southeast1"
PROJECT = os.environ.get("GCP_PROJECT", "agora-data-driven")
BUCKET = os.environ.get("STAGING_BUCKET", "agora-data-driven-staging")
LIFECYCLE_DAYS = 30


def main() -> None:
    gcs = storage.Client(project=PROJECT)

    bucket = gcs.bucket(BUCKET)
    if bucket.exists():
        print(f"[OK] bucket already exists: gs://{BUCKET}")
    else:
        bucket.storage_class = "STANDARD"
        # Uniform bucket-level access: IAM is the only access path, and no object can be
        # given its own public ACL. Raw client performance data is never public.
        bucket.iam_configuration.uniform_bucket_level_access_enabled = True
        bucket = gcs.create_bucket(bucket, location=LOCATION, project=PROJECT)
        print(f"[OK] created gs://{BUCKET} ({LOCATION}, uniform access)")

    # Idempotent: setting the same rule twice converges.
    bucket.add_lifecycle_delete_rule(age=LIFECYCLE_DAYS)
    bucket.patch()
    print(f"[OK] lifecycle: delete objects older than {LIFECYCLE_DAYS} days")
    print(f"     public access prevention: {bucket.iam_configuration.public_access_prevention}")


if __name__ == "__main__":
    main()
