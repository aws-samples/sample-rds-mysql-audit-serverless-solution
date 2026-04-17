# RDS MySQL Audit Serverless Solution

A serverless solution for collecting, storing, and querying audit logs from Amazon Aurora MySQL and Amazon RDS for MySQL. Deploys via AWS CDK or manual console setup.

## Features

- **Multi-database support** — Aurora MySQL clusters, RDS MySQL standalone instances, and RDS MySQL Multi-AZ DB clusters
- **Auto-discovery** — Daily Lambda scan detects new databases with audit logging enabled and updates the collection config automatically
- **Multi-cluster collection** — Dispatcher-Worker architecture processes multiple databases in parallel with fault isolation
- **S3 archival** — Audit logs stored in S3 with cluster/date/instance path partitioning
- **Athena integration** — Pre-built Glue table and 7 saved queries for immediate SQL analysis
- **Dual timestamp support** — Queries handle both Aurora (microsecond Unix) and RDS MySQL (`YYYYMMDD HH:MM:SS`) timestamp formats
- **No VPC required** — Uses RDS management plane APIs, works without VPC connectivity to database endpoints
- **Deduplication** — DynamoDB state tracking prevents duplicate log uploads
- **Least-privilege IAM** — Each Lambda has its own role with minimal permissions

## Architecture

```
┌──────────────┐    cron(daily)    ┌───────────────-────┐
│  EventBridge │──────────────────▶│  Discovery Lambda  │──▶ Scan RDS/Aurora
└──────────────┘                   └────────┬───────-───┘    Update config
                                            ▼
┌──────────────┐    rate(N min)    ┌────────────-─────┐
│  EventBridge │──────────────────▶│ Dispatcher Lambda│
└──────────────┘                   └────────┬────-────┘
                                            │ Read S3 config
                          ┌─────────────────┼─────────────────┐
                          ▼                 ▼                 ▼
                   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
                   │   Worker    │   │   Worker    │   │   Worker    │
                   │ (Cluster A) │   │ (Cluster B) │   │ (Instance C)│
                   └──────┬──────┘   └──────┬──────┘   └──────┬──────┘
                          │                 │                 │
                          ▼                 ▼                 ▼
              ┌──────────────────────────────────────────────────────┐
              │  S3: audit-logs/{cluster_id}/{YYYY/MM/DD}/{instance} │
              │  DynamoDB: state tracking (deduplication)            │
              │  Glue + Athena: SQL queries on audit logs            │
              └──────────────────────────────────────────────────────┘
```

**Discovery Lambda** scans the region daily for Aurora MySQL clusters and/or RDS MySQL instances with audit logging enabled, generates `cluster_config.json` on S3. **Dispatcher Lambda** reads the config every N minutes and asynchronously invokes a **Worker Lambda** per database. Workers pull audit logs via the RDS `DownloadDBLogFilePortion` API and upload to S3, using DynamoDB to track processed files.

## Supported Database Types

| Database Type | Audit Mechanism | Discovery Method |
|--------------|----------------|-----------------|
| Aurora MySQL cluster | `server_audit_logging` parameter | `DescribeDBClusters` (engine=aurora-mysql) |
| RDS MySQL instance (Single-AZ / Multi-AZ instance) | MariaDB Audit Plugin (option group) | `DescribeDBInstances` (engine=mysql) |
| RDS MySQL Multi-AZ DB cluster | `server_audit_logging` parameter | `DescribeDBClusters` (engine=mysql) |

Configure `target_rds_type` in `cdk.context.json`:
- `aurora-mysql` — Aurora MySQL only (default)
- `rds-mysql` — RDS MySQL only (standalone + Multi-AZ DB clusters)
- `both` — All MySQL databases

## Prerequisites

- Python 3.12+
- AWS CDK CLI (`npm install -g aws-cdk`)
- AWS CLI configured with credentials
- An S3 bucket for storing audit logs (CDK does not create it)
- Target databases with audit logging enabled:
  - Aurora MySQL: `server_audit_logging=ON` in cluster parameter group
  - RDS MySQL: MariaDB Audit Plugin added to option group

## Quick Start

```bash
git clone https://github.com/aws-samples/sample-rds-mysql-audit-serverless-solution.git
cd sample-rds-mysql-audit-serverless-solution

# Set up virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Copy from cdk.context.json.example and edit cdk.context.json with your settings (at minimum: s3_bucket_name, region)
cp cdk.context.json.example cdk.context.json

# Bootstrap CDK (first time only)
cdk bootstrap aws://<ACCOUNT_ID>/<REGION>

# Deploy
cdk deploy
```

After deployment, trigger the first discovery manually:

```bash
aws lambda invoke --function-name rds-audit-cluster-discovery --region <region> /dev/stdout
```

Or use the local script:

```bash
python scripts/discover_clusters.py \
  --target-rds-type both \
  --region <region> \
  --upload s3://<your-bucket>/config/cluster_config.json
```

> For manual console deployment without CDK, see [CONSOLE_DEPLOY_GUIDE.md](CONSOLE_DEPLOY_GUIDE.md).

## Configuration

Edit `cdk.context.json`:

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `s3_bucket_name` | Yes | — | S3 bucket for audit logs |
| `region` | No | `us-west-1` | Deployment region |
| `target_rds_type` | No | `aurora-mysql` | `aurora-mysql`, `rds-mysql`, or `both` |
| `config_s3_key` | No | `config/cluster_config.json` | S3 key for cluster config |
| `dispatcher_schedule_minutes` | No | 5 | Dispatcher invocation interval |
| `lambda_memory_mb` | No | 512 | Worker Lambda memory (MB) |
| `lambda_timeout_seconds` | No | 300 | Worker Lambda timeout (seconds) |
| `dispatcher_memory_mb` | No | 256 | Dispatcher Lambda memory (MB) |
| `discovery_memory_mb` | No | 256 | Discovery Lambda memory (MB) |
| `discovery_timeout_seconds` | No | 120 | Discovery Lambda timeout (seconds) |
| `glue_database_name` | No | `rds_audit_logs` | Glue database name for Athena |
| `glue_table_name` | No | `audit_logs` | Glue table name |

## Deployed Resources

| Resource | Name | Purpose |
|----------|------|---------|
| Lambda | `rds-audit-log-retriever` | Worker: pull audit logs, upload to S3 |
| Lambda | `rds-audit-log-dispatcher` | Dispatcher: read config, invoke Workers |
| Lambda | `rds-audit-cluster-discovery` | Discovery: daily scan, update config |
| EventBridge Rule | `rds-audit-dispatcher-schedule` | Trigger Dispatcher every N minutes |
| EventBridge Rule | `rds-audit-cluster-discovery-schedule` | Trigger Discovery daily at 02:00 UTC |
| DynamoDB Table | `rds-audit-log-state` | Track processed log files |
| Glue Database | `rds_audit_logs` | Athena query database |
| Glue Table | `audit_logs` | External table over S3 audit logs |
| Athena Saved Queries | × 7 | Pre-built audit queries |

## Querying Audit Logs with Athena

Seven pre-built saved queries are deployed automatically:

| Query | Description |
|-------|-------------|
| Query By User | Filter by username |
| Query By Time Range | Filter by time range |
| Failed Operations | Find failed operations (retcode ≠ 0) |
| DDL Operations | CREATE, ALTER, DROP, TRUNCATE |
| DML Operations | INSERT, UPDATE, DELETE |
| DCL Operations | GRANT, REVOKE |
| User Operation Statistics | Operation counts per user |

All queries auto-detect the timestamp format (Aurora microsecond Unix vs RDS MySQL `YYYYMMDD HH:MM:SS`).

## Cluster Config File Format

`cluster_config.json` is stored on S3 and read by the Dispatcher:

```json
{
  "version": "1.0",
  "generated_at": "2026-01-01T00:00:00Z",
  "region": "us-west-1",
  "clusters": [
    {
      "cluster_id": "prod-aurora-cluster-1",
      "instance_ids": ["prod-instance-1a", "prod-instance-1b"],
      "enabled": true,
      "type": "aurora-mysql"
    },
    {
      "cluster_id": "my-rds-instance",
      "instance_ids": ["my-rds-instance"],
      "enabled": true,
      "type": "rds-mysql"
    }
  ]
}
```

- `enabled: false` entries are skipped by the Dispatcher
- Discovery Lambda auto-updates this file daily; new databases are added automatically
- Manually set `enabled: false` flags are preserved across Discovery updates
- The `type` field is informational (`aurora-mysql`, `rds-mysql`, `rds-mysql-multi-az-cluster`)

## S3 Path Structure

```
s3://<bucket>/audit-logs/{cluster_id}/{YYYY/MM/DD}/{instance_id}/{log_filename}
```

## Key Design Decisions

- **No VPC attachment** — Lambda calls RDS management plane APIs (`rds.<region>.amazonaws.com`), not database endpoints. Works without VPC connectivity.
- **No CloudWatch Logs export** — Avoids `$0.67/GB` CW Logs ingestion cost. Logs go directly from RDS API to S3.
- **Fault isolation** — Each Worker invocation is independent. One cluster failure doesn't affect others.
- **Config preservation** — Discovery preserves manually set `enabled: false` flags when updating the config.

## Project Structure

```
rds-mysql-audit-serverless-solution/
├── lambda_code/
│   ├── index.py              # Worker Lambda
│   ├── dispatcher.py         # Dispatcher Lambda
│   ├── cluster_discovery.py  # Discovery Lambda
│   └── config_model.py       # Config parsing & validation
├── scripts/
│   └── discover_clusters.py  # Local discovery script
├── stack.py                  # CDK Stack
├── app.py                    # CDK App entry point
├── cdk.context.json.example  # Deployment parameters
├── cdk.json                  # CDK config
├── requirements.txt          # Python dependencies
├── CONSOLE_DEPLOY_GUIDE.md   # Manual console deployment guide
└── README.md                 # Solution documentation (Chinese)
```

## Cleanup

```bash
cdk destroy
```

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.
