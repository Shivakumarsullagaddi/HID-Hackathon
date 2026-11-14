"""AWS Cloud Integration: S3 Central Audit Sync & CloudWatch Observability.

Provides non-blocking background synchronization of audit logs, gallery backups,
and custom CloudWatch metrics/structured log shipping.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid

logger = logging.getLogger("spoof.aws_cloud")

# Auto-load .env.aws if present and AWS credentials not in environment
_env_file = Path(__file__).resolve().parent.parent / ".env.aws"
if _env_file.exists():
    try:
        with open(_env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k not in os.environ:
                        os.environ[k] = v
    except Exception as e:
        logger.debug(f"Failed to read .env.aws: {e}")

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="aws_cloud")


class AWSCloudService:
    def __init__(self):
        self.region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        self.bucket_name = os.environ.get("S3_AUDIT_BUCKET", "spot-a-spoof-audit-010526273048")
        self.cw_namespace = os.environ.get("CLOUDWATCH_NAMESPACE", "SpotASpoof/AccessControl")
        self.log_group_name = os.environ.get("CLOUDWATCH_LOG_GROUP", "/spot-a-spoof/decisions")

        self.s3_client = None
        self.cw_client = None
        self.logs_client = None
        self.available = False
        self._init_clients()

    def _init_clients(self):
        try:
            # Disable cloud sync during automated test runs
            if "PYTEST_CURRENT_TEST" in os.environ:
                self.available = False
                return

            import boto3
            # Check if basic credentials exist
            if not os.environ.get("AWS_ACCESS_KEY_ID"):
                self.available = False
                return

            self.s3_client = boto3.client("s3", region_name=self.region)
            self.cw_client = boto3.client("cloudwatch", region_name=self.region)
            self.logs_client = boto3.client("logs", region_name=self.region)
            self.available = True
        except Exception as e:
            self.available = False
            logger.debug(f"AWS Cloud Service disabled or boto3 init failed: {e}")

    # ---- S3 Backup & Sync ------------------------------------------------
    def backup_to_s3(
        self,
        decisions_path: Optional[Path] = None,
        gallery_path: Optional[Path] = None,
        offline_batch: Optional[List[Dict[str, Any]]] = None,
    ):
        """Asynchronously backs up audit logs and gallery to S3."""
        if "PYTEST_CURRENT_TEST" in os.environ or not self.available or not self.s3_client:
            return
        _executor.submit(self._do_backup_to_s3, decisions_path, gallery_path, offline_batch)

    def _do_backup_to_s3(
        self,
        decisions_path: Optional[Path],
        gallery_path: Optional[Path],
        offline_batch: Optional[List[Dict[str, Any]]],
    ):
        try:
            today_str = datetime.now(timezone.utc).strftime("%Y%m%d")

            # 1. Decisions JSONL backup
            if decisions_path and decisions_path.exists():
                self.s3_client.upload_file(
                    str(decisions_path),
                    self.bucket_name,
                    "audit/decisions.jsonl",
                )
                self.s3_client.upload_file(
                    str(decisions_path),
                    self.bucket_name,
                    f"audit/history/decisions_{today_str}.jsonl",
                )

            # 2. Gallery JSON backup
            if gallery_path and gallery_path.exists():
                self.s3_client.upload_file(
                    str(gallery_path),
                    self.bucket_name,
                    "gallery/gallery.json",
                )

            # 3. Offline Batch upload (if syncing)
            if offline_batch:
                batch_id = uuid.uuid4().hex[:12]
                batch_body = "\n".join(json.dumps(r) for r in offline_batch) + "\n"
                self.s3_client.put_object(
                    Bucket=self.bucket_name,
                    Key=f"offline_syncs/batch_{today_str}_{batch_id}.jsonl",
                    Body=batch_body.encode("utf-8"),
                    ContentType="application/x-ndjson",
                )
        except Exception as e:
            logger.warning(f"S3 backup failed: {e}")

    # ---- CloudWatch Metrics ----------------------------------------------
    def publish_metrics(
        self,
        decision: str,
        latency_ms: Optional[float] = None,
        is_stepup: bool = False,
        offline_sync_count: int = 0,
    ):
        """Asynchronously records access decisions and latency into CloudWatch."""
        if "PYTEST_CURRENT_TEST" in os.environ or not self.available or not self.cw_client:
            return
        _executor.submit(
            self._do_publish_metrics,
            decision,
            latency_ms,
            is_stepup,
            offline_sync_count,
        )

    def _do_publish_metrics(
        self,
        decision: str,
        latency_ms: Optional[float],
        is_stepup: bool,
        offline_sync_count: int,
    ):
        try:
            metric_data = []

            if offline_sync_count > 0:
                metric_data.append({
                    "MetricName": "OfflineSyncCount",
                    "Value": float(offline_sync_count),
                    "Unit": "Count",
                })

            if decision:
                metric_data.append({
                    "MetricName": "VerificationCount",
                    "Value": 1.0,
                    "Unit": "Count",
                })
                if decision == "ALLOW":
                    metric_data.append({"MetricName": "AllowCount", "Value": 1.0, "Unit": "Count"})
                elif decision == "DENY":
                    metric_data.append({"MetricName": "DenyCount", "Value": 1.0, "Unit": "Count"})
                elif decision == "REQUEST_VERIFICATION" or is_stepup:
                    metric_data.append({"MetricName": "StepUpCount", "Value": 1.0, "Unit": "Count"})

            if latency_ms is not None and latency_ms > 0:
                metric_data.append({
                    "MetricName": "VerificationLatencyMs",
                    "Value": float(latency_ms),
                    "Unit": "Milliseconds",
                })

            if metric_data:
                self.cw_client.put_metric_data(
                    Namespace=self.cw_namespace,
                    MetricData=metric_data,
                )
        except Exception as e:
            logger.warning(f"CloudWatch Metrics publish failed: {e}")

    # ---- CloudWatch Structured Logs ---------------------------------------
    def log_audit_event(self, record: Dict[str, Any]):
        """Asynchronously ships a structured JSON audit event to CloudWatch Logs."""
        if "PYTEST_CURRENT_TEST" in os.environ or not self.available or not self.logs_client:
            return
        _executor.submit(self._do_log_audit_event, dict(record))

    def _do_log_audit_event(self, record: Dict[str, Any]):
        try:
            # Clean biometric vectors for privacy before shipping to cloud logs
            sanitized = {k: v for k, v in record.items() if k not in ("embedding", "frames")}
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            stream_name = f"kiosk-{date_str}"

            # Ensure log stream exists
            try:
                self.logs_client.create_log_stream(
                    logGroupName=self.log_group_name,
                    logStreamName=stream_name,
                )
            except Exception:
                pass  # Already exists

            timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
            self.logs_client.put_log_events(
                logGroupName=self.log_group_name,
                logStreamName=stream_name,
                logEvents=[{
                    "timestamp": timestamp,
                    "message": json.dumps(sanitized),
                }],
            )
        except Exception as e:
            logger.warning(f"CloudWatch Logs shipping failed: {e}")


# Singleton instance
cloud = AWSCloudService()
