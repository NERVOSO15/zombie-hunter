# 🧟 Zombie Hunter

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**A FinOps tool that hunts zombie cloud resources, estimates cost savings, and enables cleanup via Slack.**

Zombie Hunter scans your cloud infrastructure for unused resources that are silently draining your budget. It finds abandoned EBS volumes, forgotten Elastic IPs, idle load balancers, and ancient snapshots—then lets you clean them up with a single click in Slack.

## 🎯 Features

- **Multi-Cloud Support**: AWS, GCP, and Azure
- **Zombie Detection**: Finds unused resources costing you money
  - Unattached EBS/Persistent Disks
  - Unused Elastic/Static IPs
  - Idle Load Balancers (no traffic or targets)
  - Old RDS/Cloud SQL snapshots
- **Cost Estimation**: Shows potential monthly and annual savings
- **Slack Integration**: Get notified and approve deletions via Slack buttons
- **Safe by Default**: Dry-run mode and deletion confirmations
- **Kubernetes Native**: Deploy as a CronJob for scheduled scans

## 📋 Table of Contents

- [Quick Start](#-quick-start)
- [Installation](#-installation)
- [Configuration](#-configuration)
- [Usage](#-usage)
- [Slack Setup](#-slack-setup)
- [Kubernetes Deployment](#-kubernetes-deployment)
- [Cloud Provider Setup](#-cloud-provider-setup)
- [Development](#-development)
- [Contributing](#-contributing)

## 🚀 Quick Start

```bash
# Clone the repository
git clone https://github.com/yourusername/zombie-hunter.git
cd zombie-hunter

# Install dependencies
pip install -e .

# Set up AWS credentials (or use IAM roles)
export AWS_ACCESS_KEY_ID=your_key
export AWS_SECRET_ACCESS_KEY=your_secret
export AWS_DEFAULT_REGION=us-east-1

# Run a dry-run scan
zombie-hunter scan --dry-run
```

## 📦 Installation

### From Source

```bash
git clone https://github.com/yourusername/zombie-hunter.git
cd zombie-hunter
pip install -e .
```

### With Docker

```bash
docker build -t zombie-hunter .
docker run --rm \
  -e AWS_ACCESS_KEY_ID=xxx \
  -e AWS_SECRET_ACCESS_KEY=xxx \
  zombie-hunter scan
```

### Dependencies

Core dependencies are automatically installed. For specific cloud providers:

```bash
# AWS only (included by default)
pip install boto3

# Add GCP support
pip install google-cloud-compute google-cloud-monitoring

# Add Azure support
pip install azure-identity azure-mgmt-compute azure-mgmt-network
```

## ⚙️ Configuration

### Configuration File

Create a `config.yaml` file:

```yaml
scanner:
  enabled_providers:
    - aws
    # - gcp
    # - azure
  
  aws_regions:
    - us-east-1
    - us-west-2
    - eu-west-1

thresholds:
  snapshot_age_days: 90    # Snapshots older than this are zombies
  lb_idle_days: 30         # LBs with no traffic for this long are zombies
  min_cost_threshold: 1.0  # Ignore resources costing less than this

slack:
  mode: interactive        # "interactive" or "report-only"
  channel: "#finops-alerts"

dry_run: true  # Set to false to enable actual deletions
```

### Environment Variables

```bash
# Application
ZOMBIE_HUNTER_CONFIG_PATH=/path/to/config.yaml
ZOMBIE_HUNTER_DRY_RUN=true

# Slack
SLACK_BOT_TOKEN=xoxb-your-token
SLACK_SIGNING_SECRET=your-secret
SLACK_CHANNEL=#finops-alerts

# AWS
AWS_ACCESS_KEY_ID=xxx
AWS_SECRET_ACCESS_KEY=xxx
AWS_DEFAULT_REGION=us-east-1

# GCP
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
GCP_PROJECT_ID=your-project

# Azure
AZURE_SUBSCRIPTION_ID=xxx
AZURE_TENANT_ID=xxx
AZURE_CLIENT_ID=xxx
AZURE_CLIENT_SECRET=xxx
```

## 🔧 Usage

### CLI Commands

```bash
# Scan all configured providers
zombie-hunter scan

# Scan specific provider
zombie-hunter scan --provider aws

# Scan specific regions
zombie-hunter scan --provider aws --region us-east-1 --region us-west-2

# Output as JSON
zombie-hunter scan --output json

# Skip Slack notification
zombie-hunter scan --no-notify

# Show current configuration
zombie-hunter config-show

# Delete a specific resource
zombie-hunter delete vol-0abc123 \
  --provider aws \
  --type ebs_volume \
  --region us-east-1

# Start Slack interactive handler (for button clicks)
zombie-hunter serve
```

### Output Formats

**Table (default):**
```
🧟 Zombie Resources Found (Scan: abc123)
┌────────────────┬─────────────┬──────────┬───────────┬────────────┬──────────────┐
│ ID             │ Type        │ Provider │ Region    │ Reason     │ Monthly Cost │
├────────────────┼─────────────┼──────────┼───────────┼────────────┼──────────────┤
│ vol-0abc123    │ Ebs Volume  │ AWS      │ us-east-1 │ Unattached │ $40.00       │
│ eipalloc-xyz   │ Elastic Ip  │ AWS      │ us-east-1 │ Unattached │ $3.60        │
└────────────────┴─────────────┴──────────┴───────────┴────────────┴──────────────┘

Total: 2 zombies, $43.60/month potential savings
```

**JSON:**
```json
{
  "scan_id": "abc123",
  "total_zombies": 2,
  "total_monthly_savings": 43.60,
  "zombies": [...]
}
```

## 💬 Slack Setup

### 1. Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Click "Create New App" → "From scratch"
3. Name it "Zombie Hunter" and select your workspace

### 2. Configure Bot Permissions

Navigate to **OAuth & Permissions** and add these scopes:

**Bot Token Scopes:**
- `chat:write` - Post messages
- `chat:write.public` - Post to public channels

### 3. Enable Interactivity (for button clicks)

Navigate to **Interactivity & Shortcuts**:
- Enable Interactivity
- Set Request URL to your server (or use Socket Mode)

### 4. Socket Mode (Recommended)

For easier setup without exposing a public URL:

1. Go to **Socket Mode** and enable it
2. Generate an **App-Level Token** with `connections:write` scope
3. Set `SLACK_APP_TOKEN=xapp-...` environment variable

### 5. Install to Workspace

1. Go to **Install App**
2. Click "Install to Workspace"
3. Copy the **Bot User OAuth Token** (`xoxb-...`)

### 6. Get Signing Secret

1. Go to **Basic Information**
2. Copy the **Signing Secret**

### Environment Variables

```bash
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_SIGNING_SECRET=your-signing-secret
SLACK_APP_TOKEN=xapp-your-app-token  # For Socket Mode
```

## ☸️ Kubernetes Deployment

### Quick Deploy

```bash
# Create namespace and apply manifests
kubectl apply -k k8s/

# Or apply individually
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/rbac.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.yaml  # Edit with your secrets first!
kubectl apply -f k8s/cronjob.yaml
```

### Create Secrets Properly

```bash
kubectl create secret generic zombie-hunter-secrets \
  --namespace zombie-hunter \
  --from-literal=SLACK_BOT_TOKEN=xoxb-xxx \
  --from-literal=SLACK_SIGNING_SECRET=xxx
```

### Using AWS IRSA (Recommended)

For EKS, use IAM Roles for Service Accounts instead of access keys:

1. Create an IAM role with necessary permissions
2. Associate with the service account:

```yaml
# k8s/rbac.yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: zombie-hunter
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::ACCOUNT:role/zombie-hunter-role
```

### CronJob Schedule

The default schedule runs at 2 AM UTC daily. Modify in `k8s/cronjob.yaml`:

```yaml
spec:
  schedule: "0 2 * * *"  # Daily at 2 AM UTC
  # schedule: "0 */6 * * *"  # Every 6 hours
  # schedule: "0 9 * * 1"    # Weekly on Monday at 9 AM
```

## ☁️ Cloud Provider Setup

### AWS

**Required IAM Permissions:**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeVolumes",
        "ec2:DescribeAddresses",
        "ec2:DeleteVolume",
        "ec2:ReleaseAddress",
        "elasticloadbalancing:DescribeLoadBalancers",
        "elasticloadbalancing:DescribeTargetGroups",
        "elasticloadbalancing:DescribeTargetHealth",
        "elasticloadbalancing:DeleteLoadBalancer",
        "rds:DescribeDBSnapshots",
        "rds:DeleteDBSnapshot",
        "cloudwatch:GetMetricStatistics"
      ],
      "Resource": "*"
    }
  ]
}
```

### GCP

1. Create a service account with these roles:
   - `roles/compute.viewer`
   - `roles/compute.storageAdmin` (for deletions)
   - `roles/monitoring.viewer`

2. Download the JSON key and set:
   ```bash
   export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json
   export GCP_PROJECT_ID=your-project
   ```

### Azure

1. Create a Service Principal:
   ```bash
   az ad sp create-for-rbac --name zombie-hunter
   ```

2. Assign Reader and Contributor roles to subscriptions

3. Set environment variables:
   ```bash
   export AZURE_SUBSCRIPTION_ID=xxx
   export AZURE_TENANT_ID=xxx
   export AZURE_CLIENT_ID=xxx
   export AZURE_CLIENT_SECRET=xxx
   ```

## 🛠️ Development

### Setup

```bash
# Clone and install in development mode
git clone https://github.com/yourusername/zombie-hunter.git
cd zombie-hunter
pip install -e ".[dev]"

# Run tests
pytest

# Run linter
ruff check .

# Run type checker
mypy zombie_hunter
```

### Project Structure

```
zombie-hunter/
├── zombie_hunter/
│   ├── __init__.py
│   ├── main.py           # CLI entry point
│   ├── config.py         # Configuration management
│   ├── scanners/
│   │   ├── base.py       # Abstract scanner interface
│   │   ├── aws.py        # AWS implementation
│   │   ├── gcp.py        # GCP implementation
│   │   └── azure.py      # Azure implementation
│   ├── resources/
│   │   └── types.py      # Data models
│   ├── cost/
│   │   └── estimator.py  # Cost calculation
│   └── slack/
│       ├── notifier.py   # Slack notifications
│       └── interactive.py # Button handlers
├── k8s/                   # Kubernetes manifests
├── tests/
├── Dockerfile
├── pyproject.toml
└── README.md
```

## 🤝 Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- Inspired by the need to reduce cloud waste
- Built for FinOps practitioners everywhere
- Thanks to all contributors!

---

**Stop paying for zombies. Hunt them down.** 🧟‍♂️🔫
