# RDS MySQL 审计日志管理方案

基于 AWS Serverless 架构的 Aurora MySQL / RDS MySQL 审计日志自动采集、存储和查询方案。支持 AWS CDK 一键部署或控制台手动部署。

## 功能特性

- **多数据库支持** — Aurora MySQL 集群、RDS MySQL 单实例、RDS MySQL Multi-AZ DB 集群
- **自动发现** — Discovery Lambda 每日扫描区域内已开启审计日志的数据库，自动更新采集配置
- **多集群并行采集** — Dispatcher-Worker 架构，多个数据库并行处理，单个失败不影响其他
- **S3 归档** — 审计日志按 集群/日期/实例 路径分区存储到 S3
- **Athena 查询** — 自动创建 Glue 外表和 7 个预定义查询，开箱即用
- **双时间戳格式** — 查询自动识别 Aurora（微秒 Unix 时间戳）和 RDS MySQL（`YYYYMMDD HH:MM:SS`）两种格式
- **无需 VPC** — 使用 RDS 管理面 API，无需连接数据库端点
- **去重机制** — DynamoDB 状态跟踪，避免重复上传
- **最小权限 IAM** — 每个 Lambda 独立角色，最小权限原则

## 架构

```
┌──────────────┐    cron(每日)      ┌─────────────-──────┐
│  EventBridge │──────────────────▶│  Discovery Lambda  │──▶ 扫描 RDS/Aurora
└──────────────┘                   └────────┬──────-────┘    更新配置
                                            ▼
┌──────────────┐    rate(N 分钟)    ┌──────────────-───┐
│  EventBridge │──────────────────▶│ Dispatcher Lambda│
└──────────────┘                   └────────┬───────-─┘
                                            │ 读取 S3 配置
                          ┌─────────────────┼─────────────────┐
                          ▼                 ▼                 ▼
                   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
                   │   Worker    │   │   Worker    │   │   Worker    │
                   │ (集群 A)     │   │ (集群 B)    │   │ (实例 C)     │
                   └──────┬──────┘   └──────┬──────┘   └──────┬──────┘
                          │                 │                 │
                          ▼                 ▼                 ▼
              ┌──────────────────────────────────────────────────────┐
              │  S3: audit-logs/{cluster_id}/{YYYY/MM/DD}/{instance} │
              │  DynamoDB: 状态跟踪（去重）                             │
              │  Glue + Athena: SQL 查询审计日志                       │
              └──────────────────────────────────────────────────────┘
```

**Discovery Lambda** 每日扫描区域内的 Aurora MySQL 集群和/或 RDS MySQL 实例，检查审计日志是否开启，生成 S3 上的 `cluster_config.json`。**Dispatcher Lambda** 每 N 分钟读取配置，为每个数据库异步调用 **Worker Lambda**。Worker 通过 RDS `DownloadDBLogFilePortion` API 拉取审计日志并上传到 S3，使用 DynamoDB 跟踪已处理文件。

## 支持的数据库类型

| 数据库类型 | 审计机制 | 发现方式 |
|-----------|---------|---------|
| Aurora MySQL 集群 | `server_audit_logging` 参数 | `DescribeDBClusters` (engine=aurora-mysql) |
| RDS MySQL 实例（单 AZ / Multi-AZ instance） | MariaDB Audit Plugin（选项组） | `DescribeDBInstances` (engine=mysql) |
| RDS MySQL Multi-AZ DB 集群 | `server_audit_logging` 参数 | `DescribeDBClusters` (engine=mysql) |

通过 `cdk.context.json` 中的 `target_rds_type` 配置：
- `aurora-mysql` — 仅 Aurora MySQL（默认）
- `rds-mysql` — 仅 RDS MySQL（单实例 + Multi-AZ DB 集群）
- `both` — 所有 MySQL 数据库

## 前置条件

- Python 3.12+
- AWS CDK CLI (`npm install -g aws-cdk`)
- AWS CLI 已配置凭证
- 已有 S3 桶用于存储审计日志（CDK 不会创建）
- 目标数据库已开启审计日志：
  - Aurora MySQL: 集群参数组中 `server_audit_logging=ON`
  - RDS MySQL: 选项组中已添加 MariaDB Audit Plugin

## 快速开始

```bash
git clone https://github.com/jet1350/rds-mysql-audit-serverless-solution.git
cd rds-mysql-audit-serverless-solution

# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 从 cdk.context.json.example 复制和编辑 cdk.context.json（至少设置 s3_bucket_name 和 region）

# CDK 引导（首次部署）
cdk bootstrap aws://<ACCOUNT_ID>/<REGION>

# 部署
cdk deploy
```

部署完成后，手动触发首次发现：

```bash
aws lambda invoke --function-name rds-audit-cluster-discovery --region <region> /dev/stdout
```

或使用本地脚本：

```bash
python scripts/discover_clusters.py \
  --target-rds-type both \
  --region <region> \
  --upload s3://<your-bucket>/config/cluster_config.json
```

> 不使用 CDK？参考 [CONSOLE_DEPLOY_GUIDE.md](CONSOLE_DEPLOY_GUIDE.md) 通过 AWS 控制台手动部署。

## 配置参数

编辑 `cdk.context.json`：

| 参数 | 必填 | 默认值 | 说明 |
|------|:---:|--------|------|
| `s3_bucket_name` | 是 | — | 存储审计日志的 S3 桶名 |
| `region` | 否 | `us-west-1` | 部署区域 |
| `target_rds_type` | 否 | `aurora-mysql` | `aurora-mysql`、`rds-mysql`、`both` |
| `config_s3_key` | 否 | `config/cluster_config.json` | 集群配置文件的 S3 路径 |
| `dispatcher_schedule_minutes` | 否 | 5 | Dispatcher 调度间隔（分钟） |
| `lambda_memory_mb` | 否 | 512 | Worker Lambda 内存（MB） |
| `lambda_timeout_seconds` | 否 | 300 | Worker Lambda 超时（秒） |
| `dispatcher_memory_mb` | 否 | 256 | Dispatcher Lambda 内存（MB） |
| `discovery_memory_mb` | 否 | 256 | Discovery Lambda 内存（MB） |
| `discovery_timeout_seconds` | 否 | 120 | Discovery Lambda 超时（秒） |
| `glue_database_name` | 否 | `rds_audit_logs` | Glue 数据库名称 |
| `glue_table_name` | 否 | `audit_logs` | Glue 表名称 |

## 部署的资源

| 资源 | 名称 | 说明 |
|------|------|------|
| Lambda | `rds-audit-log-retriever` | Worker：拉取审计日志，上传 S3 |
| Lambda | `rds-audit-log-dispatcher` | Dispatcher：读取配置，调度 Worker |
| Lambda | `rds-audit-cluster-discovery` | Discovery：每日扫描，更新配置 |
| EventBridge 规则 | `rds-audit-dispatcher-schedule` | 每 N 分钟触发 Dispatcher |
| EventBridge 规则 | `rds-audit-cluster-discovery-schedule` | 每日 02:00 UTC 触发 Discovery |
| DynamoDB 表 | `rds-audit-log-state` | 记录已处理的日志文件 |
| Glue Database | `rds_audit_logs` | Athena 查询用数据库 |
| Glue Table | `audit_logs` | 映射 S3 审计日志的外表 |
| Athena Saved Queries | × 7 | 预定义审计查询 |

## 集群配置文件格式

`cluster_config.json` 存放在 S3 上，由 Dispatcher 读取：

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

- `enabled: false` 的条目会被 Dispatcher 跳过
- Discovery Lambda 每日自动更新，新数据库自动加入
- 手动设置的 `enabled: false` 在 Discovery 更新时会被保留
- `type` 字段为信息字段（`aurora-mysql`、`rds-mysql`、`rds-mysql-multi-az-cluster`）

## Athena 查询审计日志

部署自动创建 Glue 数据库、外表和 7 个预定义查询：

| 查询名称 | 用途 |
|----------|------|
| Query By User | 按用户名筛选 |
| Query By Time Range | 按时间范围筛选 |
| Failed Operations | 查询失败操作（retcode ≠ 0） |
| DDL Operations | CREATE/ALTER/DROP/TRUNCATE |
| DML Operations | INSERT/UPDATE/DELETE |
| DCL Operations | GRANT/REVOKE |
| User Operation Statistics | 按用户统计操作次数 |

所有查询自动识别 Aurora（微秒 Unix 时间戳）和 RDS MySQL（`YYYYMMDD HH:MM:SS`）两种时间戳格式。

## S3 路径结构

```
s3://<bucket>/audit-logs/{cluster_id}/{YYYY/MM/DD}/{instance_id}/{log_filename}
```

## 关键设计决策

- **无需 VPC** — Lambda 调用 RDS 管理面 API（`rds.<region>.amazonaws.com`），不连接数据库端点
- **不使用 CloudWatch Logs 导出** — 避免 $0.67/GB 的 CW Logs 摄入费用，日志直接从 RDS API 到 S3
- **故障隔离** — 每个 Worker 调用独立，单个集群失败不影响其他
- **配置保留** — Discovery 更新配置时保留手动设置的 `enabled: false` 标记

## 项目结构

```
rds-mysql-audit-serverless-solution/
├── lambda_code/
│   ├── index.py              # Worker Lambda
│   ├── dispatcher.py         # Dispatcher Lambda
│   ├── cluster_discovery.py  # Discovery Lambda
│   └── config_model.py       # 配置解析与验证
├── scripts/
│   └── discover_clusters.py  # 本地发现脚本
├── stack.py                  # CDK Stack
├── app.py                    # CDK App 入口
├── cdk.context.json.example  # 部署参数
├── cdk.json                  # CDK 配置
├── requirements.txt          # Python 依赖
├── CONSOLE_DEPLOY_GUIDE.md   # 控制台手动部署指南
└── README.md
```

## 清理

```bash
cdk destroy
```

## 注意事项

- 本方案不修改 Aurora 参数组或 RDS 选项组，需确保目标数据库审计日志已开启
- 不开启 `server_audit_logs_upload` 和 CloudWatch Logs Export，避免额外费用
- S3 桶需提前创建，CDK 不会创建或删除该桶
- Dispatcher 返回调度摘要（triggered / skipped / failed），可通过 CloudWatch Logs 监控

## 安全

更多信息请参阅 [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications)。

## 许可证

本项目基于 MIT-0 许可证授权，详见 LICENSE 文件。
