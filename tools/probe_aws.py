import os
import sys
from pathlib import Path

# Load .env.aws if exists
env_path = Path(__file__).resolve().parent.parent / ".env.aws"
if env_path.exists():
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

import boto3
from botocore.exceptions import ClientError

region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
print(f"--- Probing AWS Sandbox (Region: {region}) ---")

# 1. STS Caller Identity
try:
    sts = boto3.client("sts", region_name=region)
    ident = sts.get_caller_identity()
    print(f"STS: Account={ident.get('Account')}, Arn={ident.get('Arn')}, UserId={ident.get('UserId')}")
except Exception as e:
    print(f"STS Failed: {e}")
    sys.exit(1)

# 2. S3
try:
    s3 = boto3.client("s3", region_name=region)
    buckets = s3.list_buckets().get("Buckets", [])
    print(f"S3: Success! Found {len(buckets)} buckets: {[b['Name'] for b in buckets[:5]]}")
except Exception as e:
    print(f"S3 Failed: {e}")

# 3. Rekognition
try:
    rek = boto3.client("rekognition", region_name=region)
    colls = rek.list_collections().get("CollectionIds", [])
    print(f"Rekognition: Success! Found {len(colls)} collections: {colls[:5]}")
except Exception as e:
    print(f"Rekognition Failed: {e}")

# 4. CloudWatch Logs
try:
    logs = boto3.client("logs", region_name=region)
    groups = logs.describe_log_groups(limit=5).get("logGroups", [])
    print(f"CloudWatch Logs: Success! Found {len(groups)} log groups: {[g['logGroupName'] for g in groups]}")
except Exception as e:
    print(f"CloudWatch Logs Failed: {e}")

# 5. CloudWatch Metrics
try:
    cw = boto3.client("cloudwatch", region_name=region)
    m = cw.list_metrics(Namespace="AWS/EC2").get("Metrics", [])
    print(f"CloudWatch Metrics: Success! Can list metrics ({len(m)} sample EC2 metrics)")
except Exception as e:
    print(f"CloudWatch Metrics Failed: {e}")

# 6. EC2
try:
    ec2 = boto3.client("ec2", region_name=region)
    vpcs = ec2.describe_vpcs().get("Vpcs", [])
    subnets = ec2.describe_subnets().get("Subnets", [])
    instances = ec2.describe_instances().get("Reservations", [])
    print(f"EC2: Success! Vpcs={len(vpcs)}, Subnets={len(subnets)}, Existing Reservations={len(instances)}")
except Exception as e:
    print(f"EC2 Failed: {e}")

# 7. ECR / App Runner
try:
    ecr = boto3.client("ecr", region_name=region)
    repos = ecr.describe_repositories().get("repositories", [])
    print(f"ECR: Success! Repos={len(repos)}")
except Exception as e:
    print(f"ECR check: {e}")

try:
    apprunner = boto3.client("apprunner", region_name=region)
    svcs = apprunner.list_services().get("ServiceSummaryList", [])
    print(f"App Runner: Success! Services={len(svcs)}")
except Exception as e:
    print(f"App Runner check: {e}")
