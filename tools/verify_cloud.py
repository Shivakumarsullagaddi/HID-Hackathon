import os
from pathlib import Path
import boto3

env_path = Path(__file__).resolve().parent.parent / ".env.aws"
if env_path.exists():
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
bucket_name = "spot-a-spoof-audit-010526273048"

s3 = boto3.client("s3", region_name=region)
logs = boto3.client("logs", region_name=region)
cw = boto3.client("cloudwatch", region_name=region)

print(f"=== AWS Cloud Verification ({region}) ===")

# 1. S3 Audit Backups
print(f"\n1. S3 Bucket ({bucket_name}):")
res = s3.list_objects_v2(Bucket=bucket_name)
for o in res.get("Contents", []):
    print(f"  ✓ {o['Key']} ({o['Size']} bytes, modified {o['LastModified']})")

# 2. CloudWatch Logs
print("\n2. CloudWatch Logs (/spot-a-spoof/decisions):")
streams = logs.describe_log_streams(logGroupName="/spot-a-spoof/decisions").get("logStreams", [])
print(f"  Log Streams ({len(streams)}): {[s['logStreamName'] for s in streams]}")
if streams:
    events = logs.get_log_events(
        logGroupName="/spot-a-spoof/decisions",
        logStreamName=streams[0]["logStreamName"],
        limit=5,
    ).get("events", [])
    print(f"  Recent Log Events count: {len(events)}")
    for ev in events[-3:]:
        print(f"    - {ev['message']}")

# 3. CloudWatch Metrics
print("\n3. CloudWatch Metrics (SpotASpoof/AccessControl):")
metrics = cw.list_metrics(Namespace="SpotASpoof/AccessControl").get("Metrics", [])
print(f"  Metrics published: {[m['MetricName'] for m in metrics]}")
