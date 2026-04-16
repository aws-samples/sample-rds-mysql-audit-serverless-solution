"""
Aurora/RDS MySQL Audit Log Retriever Lambda

Retrieves audit log files from Aurora/RDS MySQL instances via RDS API,
uploads to S3, and tracks state in DynamoDB to avoid duplicates.

Supports two invocation modes:
  1. Event payload mode: Dispatcher Lambda passes ``cluster_id`` (str) and
     ``instance_ids`` (list[str]) in the event. S3 paths include the
     cluster_id prefix: ``audit-logs/{cluster_id}/{date}/{instance_id}/...``
  2. Environment variable mode (backward-compatible): instance IDs are read
     from the ``INSTANCE_IDS`` env var; ``cluster_id`` defaults to
     ``"default"`` and S3 paths remain unchanged.

Supports multipart upload for large log files (>5MB).
"""
import boto3
import logging
import os
import json
import re
from datetime import datetime, timedelta

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# RDS identifiers: letters, digits, hyphens only (no slashes or dots)
_SAFE_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9\-]{0,62}$')


def _validate_rds_id(value: str, label: str) -> str:
    """Validate cluster_id / instance_id used in S3 key construction.

    Allows only RDS-legal identifier characters (alphanumeric + hyphen).
    Raises ValueError on any suspicious value.
    """
    if not _SAFE_ID_RE.match(value):
        raise ValueError(
            f"Rejected unsafe value for '{label}': {value!r}. "
            "Only alphanumeric characters and hyphens are allowed."
        )
    return value


def _validate_log_filename(value: str) -> str:
    """Validate log_filename used in S3 key construction.

    Allows path separators (e.g. 'audit/server_audit.log') but rejects
    '..' components, absolute paths, and null bytes to prevent traversal.
    """
    if not value or not isinstance(value, str):
        raise ValueError("log_filename must be a non-empty string")
    if "\x00" in value:
        raise ValueError(f"Rejected log_filename with null byte: {value!r}")
    if value.startswith("/"):
        raise ValueError(f"Rejected absolute log_filename: {value!r}")
    if any(part == ".." for part in value.replace("\\", "/").split("/")):
        raise ValueError(f"Rejected log_filename with path traversal: {value!r}")
    return value


def upload_to_s3_multipart(s3_client, bucket_name, s3_key, data, metadata,
                           chunk_size=5 * 1024 * 1024):
    """Upload data to S3, using multipart upload for files > 5MB."""
    data_size = len(data)

    if data_size < chunk_size:
        s3_client.put_object(
            Bucket=bucket_name, Key=s3_key, Body=data, Metadata=metadata
        )
        return

    mpu = s3_client.create_multipart_upload(
        Bucket=bucket_name, Key=s3_key, Metadata=metadata
    )
    upload_id = mpu["UploadId"]
    parts = []
    part_number = 1
    offset = 0

    try:
        while offset < data_size:
            end = min(offset + chunk_size, data_size)
            part = s3_client.upload_part(
                Bucket=bucket_name, Key=s3_key,
                PartNumber=part_number, UploadId=upload_id,
                Body=data[offset:end],
            )
            parts.append({"PartNumber": part_number, "ETag": part["ETag"]})
            offset = end
            part_number += 1

        s3_client.complete_multipart_upload(
            Bucket=bucket_name, Key=s3_key, UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except Exception as e:
        s3_client.abort_multipart_upload(
            Bucket=bucket_name, Key=s3_key, UploadId=upload_id
        )
        raise e


def lambda_handler(event, context):
    rds_client = boto3.client("rds")
    s3_client = boto3.client("s3")
    dynamodb = boto3.resource("dynamodb")

    # --- Event payload parsing (Task 1.1) ---
    event_cluster_id = event.get("cluster_id") if isinstance(event, dict) else None
    event_instance_ids = event.get("instance_ids") if isinstance(event, dict) else None

    if (event_cluster_id and isinstance(event_cluster_id, str)
            and event_instance_ids and isinstance(event_instance_ids, list)
            and len(event_instance_ids) > 0):
        # Dispatcher invocation mode
        instance_ids = event_instance_ids
        cluster_id = _validate_rds_id(event_cluster_id, "cluster_id")
        use_cluster_prefix = True
    else:
        # Backward-compatible: environment variable mode
        instance_ids = os.environ["INSTANCE_IDS"].split(",")
        cluster_id = "default"
        use_cluster_prefix = False

    bucket_name = os.environ["BUCKET_NAME"]
    state_table_name = os.environ["STATE_TABLE_NAME"]
    state_table = dynamodb.Table(state_table_name)
    lookback_minutes = int(os.environ.get("LOOKBACK_MINUTES", 10))

    total_processed = 0
    total_skipped = 0
    total_found = 0

    for instance_id in instance_ids:
        instance_id = instance_id.strip()
        try:
            _validate_rds_id(instance_id, "instance_id")
        except ValueError:
            logger.error("Skipping instance with unsafe id: %r", instance_id)
            continue
        logger.info("Processing logs from instance: %s", instance_id)

        response = rds_client.describe_db_log_files(
            DBInstanceIdentifier=instance_id,
            FilenameContains="audit",
            FileLastWritten=int(
                (datetime.now() - timedelta(minutes=lookback_minutes)).timestamp() * 1000
            ),
        )

        processed_count = 0
        skipped_count = 0

        for log_file in response["DescribeDBLogFiles"]:
            log_filename = log_file["LogFileName"]
            log_size = log_file["Size"]
            log_last_written = log_file["LastWritten"]

            try:
                _validate_log_filename(log_filename)
            except ValueError:
                logger.error("Skipping log file with unsafe name: %r", log_filename)
                continue

            # Check if already processed
            state_key = f"{instance_id}#{log_filename}"
            try:
                existing = state_table.get_item(Key={"log_file_id": state_key})
                if "Item" in existing:
                    item = existing["Item"]
                    if (item["last_written"] == log_last_written
                            and item["size"] == log_size 
                            and item.get("status") == "completed"):
                        skipped_count += 1
                        continue
            except Exception as e:
                logger.error("Error checking state for %s: %s", log_filename, e)

            # Download log file (paginated)
            log_data_parts = []
            marker = "0"
            while True:
                portion = rds_client.download_db_log_file_portion(
                    DBInstanceIdentifier=instance_id,
                    LogFileName=log_filename,
                    Marker=marker,
                )
                log_data_parts.append(portion["LogFileData"])
                if portion.get("AdditionalDataPending"):
                    marker = portion["Marker"]
                else:
                    break

            log_data = "".join(log_data_parts).encode("utf-8")
            date_prefix = datetime.now().strftime('%Y/%m/%d')
            if use_cluster_prefix:
                s3_key = (
                    f"audit-logs/{cluster_id}/{date_prefix}"
                    f"/{instance_id}/{log_filename}"
                )
            else:
                s3_key = (
                    f"audit-logs/{date_prefix}"
                    f"/{instance_id}/{log_filename}"
                )

            try:
                upload_to_s3_multipart(
                    s3_client=s3_client,
                    bucket_name=bucket_name,
                    s3_key=s3_key,
                    data=log_data,
                    metadata={
                        "source_instance": instance_id,
                        "log_filename": log_filename,
                        "last_written": str(log_last_written),
                        "size": str(log_size),
                    },
                )
                state_table.put_item(Item={
                    "log_file_id": state_key,
                    "instance_id": instance_id,
                    "log_filename": log_filename,
                    "last_written": log_last_written,
                    "s3_key": s3_key,
                    "size": log_size,
                    "processed_at": datetime.now().isoformat(),
                    "status": "completed",
                    "expiration_time": int((datetime.now() + timedelta(days=180)).timestamp()),
                })
                processed_count += 1
            except Exception as e:
                logger.error("Error uploading %s to S3: %s", log_filename, e)
                state_table.put_item(Item={
                    "log_file_id": state_key,
                    "instance_id": instance_id,
                    "log_filename": log_filename,
                    "last_written": log_last_written,
                    "s3_key": s3_key,
                    "size": log_size,
                    "processed_at": datetime.now().isoformat(),
                    "status": "failed",
                    "error_message": str(e),
                    "expiration_time": int((datetime.now() + timedelta(days=180)).timestamp()),
                })

        total_processed += processed_count
        total_skipped += skipped_count
        total_found += len(response["DescribeDBLogFiles"])
        logger.info(
            "Instance %s: Processed=%d, Skipped=%d, Found=%d",
            instance_id, processed_count, skipped_count,
            len(response["DescribeDBLogFiles"]),
        )

    return {
        "statusCode": 200,
        "body": json.dumps({
            "cluster_id": cluster_id,
            "processed": total_processed,
            "skipped": total_skipped,
            "total_found": total_found,
            "instances_checked": len(instance_ids),
        }),
    }
