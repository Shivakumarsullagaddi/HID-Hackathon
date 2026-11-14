"""Deploy Spot-a-Spoof to AWS EC2 in us-east-1 with HTTPS and CloudWatch/S3 integration."""

import os
import sys
import time
import tarfile
from pathlib import Path
import tempfile
import urllib.request
import json
import ssl

import boto3
from botocore.exceptions import ClientError

# Ensure environment variables from .env.aws are loaded
REPO_ROOT = Path(__file__).resolve().parent.parent
env_file = REPO_ROOT / ".env.aws"
if env_file.exists():
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
BUCKET_NAME = os.environ.get("S3_AUDIT_BUCKET", "spot-a-spoof-audit-010526273048")
SG_NAME = "spot-a-spoof-sg"
INSTANCE_TYPE = "t3.medium"
AMI_ID = "ami-05221e3867c5db247"  # Ubuntu 22.04 LTS official in us-east-1

print("=" * 65)
print(f"  Spot-a-Spoof AWS Cloud Deployer (Region: {REGION})")
print("=" * 65)

ec2 = boto3.client("ec2", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)
cw = boto3.client("cloudwatch", region_name=REGION)

# 1. Ensure S3 bucket exists
print(f"\n[1/6] Verifying S3 bucket: {BUCKET_NAME}...")
try:
    s3.head_bucket(Bucket=BUCKET_NAME)
    print(f"  ✓ S3 bucket '{BUCKET_NAME}' exists and is ready.")
except ClientError:
    s3.create_bucket(Bucket=BUCKET_NAME)
    print(f"  ✓ Created S3 bucket '{BUCKET_NAME}'.")

# 2. Ensure CloudWatch Log Group & Metrics namespace exist
print("\n[2/6] Verifying CloudWatch Observability resources...")
try:
    logs.create_log_group(logGroupName="/spot-a-spoof/decisions")
    print("  ✓ Created CloudWatch Log Group '/spot-a-spoof/decisions'.")
except logs.exceptions.ResourceAlreadyExistsException:
    print("  ✓ CloudWatch Log Group '/spot-a-spoof/decisions' ready.")
except Exception as e:
    print(f"  ! Log group note: {e}")

# 3. Package repository into tarball and upload to S3
print("\n[3/6] Packaging application bundle (code, models, web UI)...")
tar_path = Path(tempfile.gettempdir()) / "spot-a-spoof-bundle.tar.gz"

def tar_filter(tarinfo):
    name = tarinfo.name
    # Exclude venvs, git, pytest cache, env files
    if any(x in name for x in [".venv", ".git", "__pycache__", ".pytest_cache", ".env.aws", "temp_"]):
        return None
    return tarinfo

with tarfile.open(tar_path, "w:gz") as tar:
    for item in ["backend", "frontend", "static", "tools", "models", "data", "requirements.txt", "README.md"]:
        p = REPO_ROOT / item
        if p.exists():
            tar.add(p, arcname=item, filter=tar_filter)

bundle_size_mb = round(tar_path.stat().st_size / (1024 * 1024), 2)
print(f"  ✓ Application bundle packaged ({bundle_size_mb} MB).", flush=True)

try:
    s3.head_object(Bucket=BUCKET_NAME, Key="deploy/bundle.tar.gz")
    print(f"  ✓ Found existing bundle in s3://{BUCKET_NAME}/deploy/bundle.tar.gz. Ready to bootstrap.", flush=True)
except Exception:
    print(f"  Uploading bundle to s3://{BUCKET_NAME}/deploy/bundle.tar.gz ...", flush=True)
    with open(tar_path, "rb") as bf:
        s3.put_object(Bucket=BUCKET_NAME, Key="deploy/bundle.tar.gz", Body=bf.read())
    print("  ✓ Bundle uploaded successfully.", flush=True)

# Generate 12-hour presigned URL for EC2 bootstrapping
presigned_url = s3.generate_presigned_url(
    "get_object",
    Params={"Bucket": BUCKET_NAME, "Key": "deploy/bundle.tar.gz"},
    ExpiresIn=43200,
)
print("  ✓ S3 presigned deployment URL generated.", flush=True)

# 4. Configure Security Group
print(f"\n[4/6] Configuring Security Group '{SG_NAME}'...", flush=True)
vpcs = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"]
if not vpcs:
    raise RuntimeError("No default VPC found in us-east-1")
default_vpc_id = vpcs[0]["VpcId"]

sg_id = None
try:
    res = ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [SG_NAME]},
            {"Name": "vpc-id", "Values": [default_vpc_id]}
        ]
    )
    if res["SecurityGroups"]:
        sg_id = res["SecurityGroups"][0]["GroupId"]
        print(f"  ✓ Using existing Security Group: {sg_id}", flush=True)
except Exception:
    pass

if not sg_id:
    res = ec2.create_security_group(
        GroupName=SG_NAME,
        Description="Security Group for Spot-a-Spoof kiosk web app and API",
        VpcId=default_vpc_id,
    )
    sg_id = res["GroupId"]
    print(f"  ✓ Created Security Group: {sg_id}", flush=True)

    # Add ingress rules: 22, 80, 443, 8000
    ip_permissions = [
        {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH"}]},
        {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "HTTP / ACME"}]},
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "HTTPS"}]},
        {"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8000, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "FastAPI Direct"}]},
    ]
    try:
        ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=ip_permissions)
        print("  ✓ Allowed inbound ports 22, 80, 443, 8000 from 0.0.0.0/0.", flush=True)
    except ClientError as e:
        if "InvalidPermission.Duplicate" not in str(e):
            raise

# 5. Launch EC2 instance
print(f"\n[5/6] Launching EC2 instance ({INSTANCE_TYPE}, {AMI_ID})...", flush=True)

access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
session_token = os.environ.get("AWS_SESSION_TOKEN", "")

user_data_script = f"""#!/bin/bash
exec > >(tee -a /var/log/spot-a-spoof-init.log /var/log/user-data.log) 2>&1
set -ex
export DEBIAN_FRONTEND=noninteractive

echo "Starting Spot-a-Spoof Provisioning..."

# 1. Update and install system dependencies
apt-get update -y
apt-get install -y python3-pip python3-venv git curl wget tar libgl1 libglib2.0-0 libgomp1

# 2. Setup application directory
mkdir -p /opt/spot-a-spoof
cd /opt/spot-a-spoof

# Download application bundle via presigned S3 URL
curl -fsSL "{presigned_url}" -o bundle.tar.gz
tar -xzf bundle.tar.gz
rm -f bundle.tar.gz

# Inject AWS Credentials into .env.aws for S3 audit sync and CloudWatch metrics
cat << 'EOF' > /opt/spot-a-spoof/.env.aws
AWS_DEFAULT_REGION={REGION}
AWS_REGION={REGION}
AWS_ACCESS_KEY_ID={access_key}
AWS_SECRET_ACCESS_KEY={secret_key}
AWS_SESSION_TOKEN={session_token}
S3_AUDIT_BUCKET={BUCKET_NAME}
EOF

# 3. Setup Python virtualenv and install dependencies
python3 -m venv /opt/spot-a-spoof/.venv
/opt/spot-a-spoof/.venv/bin/pip install --upgrade pip
/opt/spot-a-spoof/.venv/bin/pip install -r requirements.txt

# Ensure models are ready
/opt/spot-a-spoof/.venv/bin/python tools/fetch_models.py --samples || true

# 4. Setup systemd service for FastAPI
cat << 'EOF' > /etc/systemd/system/spot-a-spoof.service
[Unit]
Description=Spot-a-Spoof FastAPI Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/spot-a-spoof
EnvironmentFile=/opt/spot-a-spoof/.env.aws
ExecStart=/opt/spot-a-spoof/.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now spot-a-spoof.service

# 5. Install standalone Caddy for zero-dependency automated TLS (Let's Encrypt / nip.io)
curl -fsSL "https://github.com/caddyserver/caddy/releases/download/v2.8.4/caddy_2.8.4_linux_amd64.tar.gz" -o /tmp/caddy.tar.gz
tar -xzf /tmp/caddy.tar.gz -C /usr/local/bin caddy
chmod +x /usr/local/bin/caddy
rm -f /tmp/caddy.tar.gz

# Retrieve public IP
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" || true)
PUBLIC_IP=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4 || curl -s ifconfig.me)

mkdir -p /etc/caddy
cat << EOF > /etc/caddy/Caddyfile
$PUBLIC_IP.nip.io {{
    reverse_proxy 127.0.0.1:8000
}}

:443 {{
    tls internal
    reverse_proxy 127.0.0.1:8000
}}

:80 {{
    reverse_proxy 127.0.0.1:8000
}}
EOF

cat << 'EOF' > /etc/systemd/system/caddy.service
[Unit]
Description=Caddy Web Server
After=network.target

[Service]
Type=simple
User=root
ExecStart=/usr/local/bin/caddy run --config /etc/caddy/Caddyfile
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now caddy.service

echo "Spot-a-Spoof setup complete!"
"""

run_res = ec2.run_instances(
    ImageId=AMI_ID,
    InstanceType=INSTANCE_TYPE,
    MinCount=1,
    MaxCount=1,
    SecurityGroupIds=[sg_id],
    UserData=user_data_script,
    BlockDeviceMappings=[
        {
            "DeviceName": "/dev/sda1",
            "Ebs": {
                "VolumeSize": 20,
                "VolumeType": "gp3",
                "DeleteOnTermination": True,
            },
        }
    ],
    TagSpecifications=[
        {
            "ResourceType": "instance",
            "Tags": [
                {"Key": "Name", "Value": "spot-a-spoof-server"},
                {"Key": "Project", "Value": "HID-Hackathon"},
            ],
        }
    ],
)

instance_id = run_res["Instances"][0]["InstanceId"]
print(f"  ✓ Instance launched: {instance_id}")

print("  Waiting for instance to initialize and receive Public IP...")
waiter = ec2.get_waiter("instance_running")
waiter.wait(InstanceIds=[instance_id])

desc = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
public_ip = desc.get("PublicIpAddress")
public_dns = desc.get("PublicDnsName")

print(f"  ✓ Instance is RUNNING!")
print(f"    Public IPv4: {public_ip}")
print(f"    Public DNS:  {public_dns}")

# 6. Monitor health check until ready
print(f"\n[6/6] Waiting for application bootstrapping (installing dependencies & starting services)...")
direct_url = f"http://{public_ip}:8000"
nip_https_url = f"https://{public_ip}.nip.io"
http_nip_url = f"http://{public_ip}.nip.io"

print(f"  Direct HTTP:  {direct_url}")
print(f"  Secure HTTPS: {nip_https_url}")

# Poll direct health check
max_attempts = 36  # up to 6 minutes
ready = False
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

for attempt in range(1, max_attempts + 1):
    time.sleep(10)
    try:
        req = urllib.request.Request(f"{direct_url}/api/status", headers={"User-Agent": "SpotASpoofDeployer"})
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status == 200:
                data = json.loads(response.read().decode("utf-8"))
                if data.get("identity_available") and data.get("liveness_mode") == "ensemble":
                    print(f"\n  ✓ Remote health check PASSED on attempt {attempt}!", flush=True)
                    print(f"    Status: {data}", flush=True)
                    ready = True
                    break
                else:
                    print(f"\n  Bootstrapping... ({attempt}/{max_attempts}) - models loading", flush=True)
    except Exception:
        sys.stdout.write(".")
        sys.stdout.flush()

if ready:
    print("\n" + "=" * 65)
    print("  🎉 DEPLOYMENT SUCCEEDED!")
    print("=" * 65)
    print(f"  Instance ID:      {instance_id}")
    print(f"  Public IP:        {public_ip}")
    print(f"  Direct Web URL:   {direct_url}/")
    print(f"  Secure HTTPS URL: {nip_https_url}/  <-- USE THIS FOR WEBCAM!")
    print(f"  S3 Audit Bucket:  s3://{BUCKET_NAME}")
    print(f"  CloudWatch Logs:  /spot-a-spoof/decisions")
    print(f"  Metrics:          Namespace 'SpotASpoof/AccessControl'")
    print("=" * 65)
else:
    print(f"\n  Note: Server is still installing packages in background.")
    print(f"  Check status in a minute at: {direct_url}/api/status")
