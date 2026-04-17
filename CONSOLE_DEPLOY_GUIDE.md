# RDS MySQL 审计日志管理方案 — AWS 控制台手动部署指南

本文档对应 CDK Stack 自动部署的所有资源，指导你通过 AWS 管理控制台完成相同的部署。

> 请将下文中的占位符替换为你的实际值：
> - `<ACCOUNT_ID>` — AWS 账户 ID
> - `<REGION>` — 部署区域（如 `us-west-1`）
> - `<S3_BUCKET>` — 审计日志 S3 桶名
> - `<CONFIG_S3_KEY>` — 集群配置文件路径（默认 `config/cluster_config.json`）

---

## 部署顺序

按依赖关系，资源创建顺序为：

1. DynamoDB 状态表
2. IAM 角色 — Worker Lambda
3. Worker Lambda 函数
4. CloudWatch 告警（Worker Lambda）
5. IAM 角色 — Dispatcher Lambda
6. Dispatcher Lambda 函数
7. EventBridge 定时规则（Dispatcher）
8. IAM 角色 — Discovery Lambda
9. Discovery Lambda 函数
10. EventBridge 每日规则（Discovery）
11. Glue 数据库与外表
12. Athena Saved Queries

---

## 步骤 1：创建 DynamoDB 状态表

**导航：** DynamoDB > 表 > 创建表

| 配置项 | 值 |
|--------|-----|
| 表名 | `rds-audit-log-state` |
| 分区键 | `log_file_id`（字符串） |
| 排序键 | 不设置 |
| 容量模式 | 按需 |

创建完成后，进入表详情 > **附加设置** > **生存时间 (TTL)** > 管理 TTL：

| 配置项 | 值 |
|--------|-----|
| TTL 属性名称 | `expiration_time` |

启用后，Lambda 写入的每条记录都会自动在 180 天后过期删除。

**验证：** 创建完成后，在表列表中确认 `rds-audit-log-state` 状态为"活动"，附加设置中 TTL 已启用。

---

## 步骤 2：创建 Worker Lambda IAM 角色

**导航：** IAM > 角色 > 创建角色

### 2.1 创建角色

| 配置项 | 值 |
|--------|-----|
| 受信任实体类型 | AWS 服务 |
| 使用案例 | Lambda |
| 角色名称 | `rds-audit-worker-role` |

### 2.2 附加权限策略

创建角色后，进入角色详情页 > 权限 > 添加内联策略，使用 JSON 编辑器粘贴以下策略：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RDSAuditLogAccess",
      "Effect": "Allow",
      "Action": [
        "rds:DescribeDBLogFiles",
        "rds:DownloadDBLogFilePortion"
      ],
      "Resource": "arn:aws:rds:<REGION>:<ACCOUNT_ID>:db:*"
    },
    {
      "Sid": "S3WriteAuditLogs",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:AbortMultipartUpload"
      ],
      "Resource": "arn:aws:s3:::<S3_BUCKET>/audit-logs/*"
    },
    {
      "Sid": "DynamoDBStateTable",
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:Query",
        "dynamodb:Scan"
      ],
      "Resource": "arn:aws:dynamodb:<REGION>:<ACCOUNT_ID>:table/rds-audit-log-state"
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:<REGION>:<ACCOUNT_ID>:*"
    }
  ]
}
```

策略名称填写 `rds-audit-worker-policy`，点击"创建策略"。

**验证：** 在角色详情页确认权限策略已附加，包含 RDS、S3、DynamoDB、CloudWatch Logs 四组权限。

---

## 步骤 3：创建 Worker Lambda 函数

**导航：** Lambda > 函数 > 创建函数

### 3.1 基本配置

| 配置项 | 值 |
|--------|-----|
| 函数名称 | `rds-audit-log-retriever` |
| 运行时 | Python 3.12 |
| 架构 | arm64 |
| 执行角色 | 使用现有角色 → `rds-audit-worker-role` |

### 3.2 常规配置

创建后进入函数配置 > 常规配置 > 编辑：

| 配置项 | 值 |
|--------|-----|
| 内存 | 512 MB |
| 超时 | 5 分 0 秒 |

### 3.3 环境变量

函数配置 > 环境变量 > 编辑：

| 键 | 值 | 说明 |
|----|-----|------|
| `BUCKET_NAME` | `<S3_BUCKET>` | 审计日志存储桶 |
| `STATE_TABLE_NAME` | `rds-audit-log-state` | DynamoDB 状态表名 |
| `LOOKBACK_MINUTES` | `10` | 向前回溯的分钟数（应为 Dispatcher 调度间隔的 2 倍，默认 10） |
| `INSTANCE_IDS` | （可选）`instance-1,instance-2` | 仅单集群模式需要；多集群模式由 Dispatcher 传入，无需设置 |

### 3.4 上传代码

函数代码 > 上传自 > .zip 文件

将 `sample-rds-mysql-audit-serverless-solution/lambda_code/` 目录下的所有 `.py` 文件打包为 zip：

```bash
cd sample-rds-mysql-audit-serverless-solution/lambda_code
zip -r ../../lambda_code.zip *.py
```

上传 `lambda_code.zip`。

> 确保 Handler 为 `index.lambda_handler`。

**验证：** 点击"测试"，创建测试事件（使用空 JSON `{}`），执行后确认无导入错误。如果未设置 `INSTANCE_IDS` 环境变量，预期会报 KeyError，这是正常的（多集群模式下由 Dispatcher 传入参数）。

---

## 步骤 4：创建 CloudWatch 告警

**导航：** CloudWatch > 告警 > 创建告警

### 4.1 Worker Lambda 错误告警

点击"选择指标" > Lambda > 按函数名称 > 选择 `rds-audit-log-retriever` 的 `Errors` 指标。

| 配置项 | 值 |
|--------|-----|
| 统计数据 | Sum |
| 时间段 | 5 分钟 |
| 条件 | 大于或等于 `1` |
| 连续评估期 | 1 |
| 缺少数据处理 | 将缺少的数据视为良好 |
| 告警名称 | `rds-audit-worker-errors` |
| 告警描述 | `Worker Lambda invocation errors` |

### 4.2 Worker Lambda 执行时长告警

点击"选择指标" > Lambda > 按函数名称 > 选择 `rds-audit-log-retriever` 的 `Duration` 指标。

| 配置项 | 值 |
|--------|-----|
| 统计数据 | p99 |
| 时间段 | 5 分钟 |
| 条件 | 大于或等于 `240000`（毫秒，即超时值 300 秒的 80%） |
| 连续评估期 | 3 |
| 缺少数据处理 | 将缺少的数据视为良好 |
| 告警名称 | `rds-audit-worker-duration-p99` |
| 告警描述 | `Worker Lambda p99 duration exceeds 80% of timeout` |

> 如果修改了 Worker Lambda 超时时间，阈值应同步调整为 `超时秒数 × 0.8 × 1000`。

**验证：** 在 CloudWatch > 告警列表中确认两个告警状态为"数据不足"（初始正常状态）。

---

## 步骤 5：创建 Dispatcher Lambda IAM 角色

**导航：** IAM > 角色 > 创建角色

### 5.1 创建角色

| 配置项 | 值 |
|--------|-----|
| 受信任实体类型 | AWS 服务 |
| 使用案例 | Lambda |
| 角色名称 | `rds-audit-dispatcher-role` |

### 5.2 附加权限策略

创建角色后，添加内联策略：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3ReadConfig",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::<S3_BUCKET>/<CONFIG_S3_KEY>"
    },
    {
      "Sid": "InvokeWorkerLambda",
      "Effect": "Allow",
      "Action": "lambda:InvokeFunction",
      "Resource": "arn:aws:lambda:<REGION>:<ACCOUNT_ID>:function:rds-audit-log-retriever"
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:<REGION>:<ACCOUNT_ID>:*"
    }
  ]
}
```

策略名称填写 `rds-audit-dispatcher-policy`。

**验证：** 确认角色包含 S3 读取、Lambda 调用、CloudWatch Logs 三组权限。

---

## 步骤 6：创建 Dispatcher Lambda 函数

**导航：** Lambda > 函数 > 创建函数

### 6.1 基本配置

| 配置项 | 值 |
|--------|-----|
| 函数名称 | `rds-audit-log-dispatcher` |
| 运行时 | Python 3.12 |
| 架构 | arm64 |
| 执行角色 | 使用现有角色 → `rds-audit-dispatcher-role` |

### 6.2 常规配置

| 配置项 | 值 |
|--------|-----|
| 内存 | 256 MB |
| 超时 | 1 分 0 秒 |

### 6.3 环境变量

| 键 | 值 | 说明 |
|----|-----|------|
| `CONFIG_BUCKET` | `<S3_BUCKET>` | 存放集群配置文件的 S3 桶 |
| `CONFIG_S3_KEY` | `<CONFIG_S3_KEY>` | 配置文件路径，默认 `config/cluster_config.json` |
| `WORKER_FUNCTION_NAME` | `rds-audit-log-retriever` | Worker Lambda 函数名 |

### 6.4 上传代码

使用与 Worker 相同的 `lambda_code.zip`（包含 `index.py`、`dispatcher.py`、`config_model.py`、`cluster_discovery.py`）。

> 确保 Handler 为 `dispatcher.lambda_handler`。

**验证：** 点击"测试"，使用空 JSON `{}` 执行。预期返回 500 错误（因为 S3 上还没有配置文件），但不应有导入错误。

---

## 步骤 7：创建 EventBridge 定时规则

### 7.1 预先创建 Dispatcher DLQ

**导航：** Amazon SQS > 队列 > 创建队列

| 配置项 | 值 |
|--------|-----|
| 队列类型 | 标准 |
| 队列名称 | `rds-audit-dispatcher-dlq` |

创建后记录队列 ARN，后续步骤 7.4 中使用。

### 7.2 规则配置

**导航：** Amazon EventBridge > 规则 > 创建规则

| 配置项 | 值 |
|--------|-----|
| 名称 | `rds-audit-dispatcher-schedule` |
| 事件总线 | default |
| 规则类型 | 计划 |

### 7.3 定义计划

选择"定期计划"，输入 rate 表达式：

```
rate(5 minutes)
```

> 可根据需要调整频率，如 `rate(10 minutes)` 或 `rate(1 hour)`。

### 7.4 选择目标

| 配置项 | 值 |
|--------|-----|
| 目标类型 | AWS 服务 |
| 选择目标 | Lambda 函数 |
| 函数 | `rds-audit-log-dispatcher` |

展开"**附加设置**"，配置重试策略和死信队列：

| 配置项 | 值 |
|--------|-----|
| 最大事件存活时间 | 5 分钟 |
| 重试尝试次数 | 2 |
| 死信队列 | SQS 队列 → `rds-audit-dispatcher-dlq` |

点击"创建规则"。

**验证：** 在规则列表中确认 `rds-audit-dispatcher-schedule` 状态为"已启用"。等待一个调度周期后，检查 Dispatcher Lambda 的 CloudWatch Logs 确认已被触发。

---

## 步骤 8：创建 Discovery Lambda IAM 角色

**导航：** IAM > 角色 > 创建角色

### 8.1 创建角色

| 配置项 | 值 |
|--------|-----|
| 受信任实体类型 | AWS 服务 |
| 使用案例 | Lambda |
| 角色名称 | `rds-audit-discovery-role` |

### 8.2 附加权限策略

创建角色后，添加内联策略：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RDSDescribeClusters",
      "Effect": "Allow",
      "Action": [
        "rds:DescribeDBClusters",
        "rds:DescribeDBClusterParameterGroups",
        "rds:DescribeDBClusterParameters",
        "rds:DescribeDBInstances",
        "rds:DescribeOptionGroups"
      ],
      "Resource": [
        "arn:aws:rds:<REGION>:<ACCOUNT_ID>:cluster:*",
        "arn:aws:rds:<REGION>:<ACCOUNT_ID>:cluster-pg:*",
        "arn:aws:rds:<REGION>:<ACCOUNT_ID>:og:*",
        "arn:aws:rds:<REGION>:<ACCOUNT_ID>:db:*"
      ]
    },
    {
      "Sid": "S3ReadWriteConfig",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject"
      ],
      "Resource": "arn:aws:s3:::<S3_BUCKET>/<CONFIG_S3_KEY>"
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:<REGION>:<ACCOUNT_ID>:*"
    }
  ]
}
```

策略名称填写 `rds-audit-discovery-policy`。

**验证：** 确认角色包含 RDS 描述、S3 读写配置、CloudWatch Logs 三组权限。

---

## 步骤 9：创建 Discovery Lambda 函数

**导航：** Lambda > 函数 > 创建函数

### 9.1 基本配置

| 配置项 | 值 |
|--------|-----|
| 函数名称 | `rds-audit-cluster-discovery` |
| 运行时 | Python 3.12 |
| 架构 | arm64 |
| 执行角色 | 使用现有角色 → `rds-audit-discovery-role` |

### 9.2 常规配置

| 配置项 | 值 |
|--------|-----|
| 内存 | 256 MB |
| 超时 | 2 分 0 秒 |

### 9.3 环境变量

| 键 | 值 | 说明 |
|----|-----|------|
| `CONFIG_BUCKET` | `<S3_BUCKET>` | 存放集群配置文件的 S3 桶 |
| `CONFIG_S3_KEY` | `<CONFIG_S3_KEY>` | 配置文件路径，默认 `config/cluster_config.json` |
| `TARGET_RDS_TYPE` | `aurora-mysql` | 目标类型：`aurora-mysql`、`rds-mysql`、`both` |

### 9.4 上传代码

使用与 Worker/Dispatcher 相同的 `lambda_code.zip`。

> 确保 Handler 为 `cluster_discovery.lambda_handler`。

**验证：** 点击"测试"，使用空 JSON `{}` 执行。预期返回 200，body 中包含 `clusters_found` 和 `config_s3_uri`。检查 S3 桶中指定目录下是否已生成 `cluster_config.json`。

---

## 步骤 10：创建 EventBridge 每日规则（Discovery）

**导航：** Amazon EventBridge > 规则 > 创建规则

### 10.1 规则配置

| 配置项 | 值 |
|--------|-----|
| 名称 | `rds-audit-cluster-discovery-schedule` |
| 事件总线 | default |
| 规则类型 | 计划 |

### 10.2 定义计划

选择"Cron 表达式"：

```
cron(0 2 * * ? *)
```

> 每日 02:00 UTC 执行。可根据需要调整时间。

### 10.3 选择目标

| 配置项 | 值 |
|--------|-----|
| 目标类型 | AWS 服务 |
| 选择目标 | Lambda 函数 |
| 函数 | `rds-audit-cluster-discovery` |

**验证：** 在规则列表中确认 `rds-audit-cluster-discovery-schedule` 状态为"已启用"。

---

## 步骤 11：创建 Glue 数据库与外表

### 11.1 创建 Glue 数据库

**导航：** AWS Glue > Data Catalog > Databases > Add database

| 配置项 | 值 |
|--------|-----|
| 数据库名称 | `rds_audit_logs` |
| 描述 | `RDS MySQL audit logs database for Athena queries` |

### 11.2 创建 Glue 外表

**导航：** Amazon Athena > Query editor

在 Athena 查询编辑器中选择数据库 `rds_audit_logs`，执行以下 SQL 创建外表：

```sql
CREATE EXTERNAL TABLE rds_audit_logs.audit_logs (
  `timestamp`    string,
  serverhost     string,
  username       string,
  host           string,
  connectionid   string,
  queryid        string,
  operation      string,
  `database`     string,
  `object`       string,
  retcode        string
)
ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.OpenCSVSerde'
WITH SERDEPROPERTIES (
  'separatorChar' = ',',
  'quoteChar' = '\'',
  'escapeChar' = '\\'
)
STORED AS INPUTFORMAT 'org.apache.hadoop.mapred.TextInputFormat'
OUTPUTFORMAT 'org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat'
LOCATION 's3://<S3_BUCKET>/audit-logs/';
```

> 所有列定义为 `string` 类型是因为 OpenCSVSerDe 的要求。数值字段在查询时通过 `CAST` 转换。
> `timestamp`、`database`、`object` 为保留字，需用反引号包裹。

**验证：** 执行 `SELECT * FROM rds_audit_logs.audit_logs LIMIT 10;`，确认能返回审计日志数据。

---

## 步骤 12：创建 Athena Saved Queries

**导航：** Amazon Athena > Saved queries > Create saved query

依次创建以下 7 个查询。每个查询的数据库选择 `rds_audit_logs`。

### 12.1 Query By User

- Name: `Audit Logs - Query By User`
- Description: `Query audit logs filtered by a specific username`

```sql
-- Replace 'target_user' with the actual username
SELECT CASE WHEN regexp_like(timestamp, '^\d+$')
            THEN from_unixtime(CAST(timestamp AS BIGINT) / 1000000)
            ELSE TRY(parse_datetime(timestamp, 'yyyyMMdd HH:mm:ss'))
       END AS event_time,
       serverhost, username, host,
       TRY_CAST(connectionid AS BIGINT) AS connectionid,
       TRY_CAST(queryid AS BIGINT) AS queryid,
       operation, database, object,
       TRY_CAST(retcode AS INTEGER) AS retcode
FROM rds_audit_logs.audit_logs
WHERE username = 'target_user'
ORDER BY timestamp DESC
LIMIT 100;
```

### 12.2 Query By Time Range

- Name: `Audit Logs - Query By Time Range`
- Description: `Query audit logs within a specific time range`

```sql
-- Query audit logs within a time range
-- Supports both Aurora (microsecond Unix ts) and RDS MySQL (YYYYMMDD HH:MM:SS)
SELECT CASE WHEN regexp_like(timestamp, '^\d+$')
            THEN from_unixtime(CAST(timestamp AS BIGINT) / 1000000)
            ELSE TRY(parse_datetime(timestamp, 'yyyyMMdd HH:mm:ss'))
       END AS event_time,
       serverhost, username, host,
       TRY_CAST(connectionid AS BIGINT) AS connectionid,
       TRY_CAST(queryid AS BIGINT) AS queryid,
       operation, database, object,
       TRY_CAST(retcode AS INTEGER) AS retcode
FROM rds_audit_logs.audit_logs
WHERE CASE WHEN regexp_like(timestamp, '^\d+$')
           THEN from_unixtime(CAST(timestamp AS BIGINT) / 1000000)
           ELSE TRY(parse_datetime(timestamp, 'yyyyMMdd HH:mm:ss'))
      END >= TIMESTAMP '2024-01-01 00:00:00'
  AND CASE WHEN regexp_like(timestamp, '^\d+$')
           THEN from_unixtime(CAST(timestamp AS BIGINT) / 1000000)
           ELSE TRY(parse_datetime(timestamp, 'yyyyMMdd HH:mm:ss'))
      END <= TIMESTAMP '2024-12-31 23:59:59'
ORDER BY timestamp DESC
LIMIT 100;
```

### 12.3 Failed Operations

- Name: `Audit Logs - Failed Operations`
- Description: `Query operations that failed (retcode != 0)`

```sql
SELECT CASE WHEN regexp_like(timestamp, '^\d+$')
            THEN from_unixtime(CAST(timestamp AS BIGINT) / 1000000)
            ELSE TRY(parse_datetime(timestamp, 'yyyyMMdd HH:mm:ss'))
       END AS event_time,
       serverhost, username, host,
       TRY_CAST(connectionid AS BIGINT) AS connectionid,
       TRY_CAST(queryid AS BIGINT) AS queryid,
       operation, database, object,
       TRY_CAST(retcode AS INTEGER) AS retcode
FROM rds_audit_logs.audit_logs
WHERE TRY_CAST(retcode AS INTEGER) != 0
ORDER BY timestamp DESC
LIMIT 100;
```

### 12.4 DDL Operations

- Name: `Audit Logs - DDL Operations`
- Description: `Query DDL operations (CREATE, ALTER, DROP, TRUNCATE)`

```sql
SELECT CASE WHEN regexp_like(timestamp, '^\d+$')
            THEN from_unixtime(CAST(timestamp AS BIGINT) / 1000000)
            ELSE TRY(parse_datetime(timestamp, 'yyyyMMdd HH:mm:ss'))
       END AS event_time,
       serverhost, username, host,
       TRY_CAST(connectionid AS BIGINT) AS connectionid,
       TRY_CAST(queryid AS BIGINT) AS queryid,
       operation, database, object,
       TRY_CAST(retcode AS INTEGER) AS retcode
FROM rds_audit_logs.audit_logs
WHERE operation IN ('CREATE', 'ALTER', 'DROP', 'TRUNCATE')
ORDER BY timestamp DESC
LIMIT 100;
```

### 12.5 DML Operations

- Name: `Audit Logs - DML Operations`
- Description: `Query DML operations (INSERT, UPDATE, DELETE)`

```sql
SELECT CASE WHEN regexp_like(timestamp, '^\d+$')
            THEN from_unixtime(CAST(timestamp AS BIGINT) / 1000000)
            ELSE TRY(parse_datetime(timestamp, 'yyyyMMdd HH:mm:ss'))
       END AS event_time,
       serverhost, username, host,
       TRY_CAST(connectionid AS BIGINT) AS connectionid,
       TRY_CAST(queryid AS BIGINT) AS queryid,
       operation, database, object,
       TRY_CAST(retcode AS INTEGER) AS retcode
FROM rds_audit_logs.audit_logs
WHERE operation IN ('INSERT', 'UPDATE', 'DELETE')
ORDER BY timestamp DESC
LIMIT 100;
```

### 12.6 User Operation Statistics

- Name: `Audit Logs - User Operation Statistics`
- Description: `Statistics of operations grouped by username and operation type`

```sql
SELECT username,
       operation,
       COUNT(*) AS operation_count,
       SUM(CASE WHEN TRY_CAST(retcode AS INTEGER) != 0 THEN 1 ELSE 0 END) AS failed_count
FROM rds_audit_logs.audit_logs
GROUP BY username, operation
ORDER BY operation_count DESC;
```

### 12.7 DCL Operations

- Name: `Audit Logs - DCL Operations`
- Description: `Query DCL operations (GRANT, REVOKE)`

```sql
SELECT CASE WHEN regexp_like(timestamp, '^\d+$')
            THEN from_unixtime(CAST(timestamp AS BIGINT) / 1000000)
            ELSE TRY(parse_datetime(timestamp, 'yyyyMMdd HH:mm:ss'))
       END AS event_time,
       serverhost, username, host,
       TRY_CAST(connectionid AS BIGINT) AS connectionid,
       TRY_CAST(queryid AS BIGINT) AS queryid,
       operation, database, object,
       TRY_CAST(retcode AS INTEGER) AS retcode
FROM rds_audit_logs.audit_logs
WHERE operation IN ('GRANT', 'REVOKE')
ORDER BY timestamp DESC
LIMIT 100;
```

**验证：** 在 Athena 控制台 > Saved queries 中确认 7 个查询均已创建。选择任意一个执行，确认返回结果正常。（！注意：执行时前两个语句需要用实际数据库用户/查询日期范围来替换WHERE语句中的条件值）

---

## 部署后配置

### 上传集群配置文件

部署完成后，Discovery Lambda 会每日 02:00 UTC 自动扫描并更新配置。首次部署可手动触发 Discovery Lambda：

```bash
aws lambda invoke --function-name rds-audit-cluster-discovery /dev/stdout
```

也可使用本地脚本手动生成并上传：

```bash
python scripts/discover_clusters.py --target-rds-type aurora-mysql --region <REGION> --upload s3://<S3_BUCKET>/<CONFIG_S3_KEY>
```

或手动创建 `cluster_config.json` 并上传到 `s3://<S3_BUCKET>/<CONFIG_S3_KEY>`：

```json
{
  "version": "1.0",
  "generated_at": "2025-01-01T00:00:00",
  "region": "<REGION>",
  "clusters": [
    {
      "cluster_id": "my-aurora-cluster",
      "instance_ids": ["my-aurora-instance-1", "my-aurora-instance-2"],
      "enabled": true,
      "type": "aurora-mysql"
    }
  ]
}
```

### 端到端验证

1. 确认 S3 桶中的指定目录下已有 `cluster_config.json`
2. 手动执行 Dispatcher Lambda（使用空 JSON `{}` 测试事件）
3. 检查 Dispatcher 返回的 `triggered` 数量是否与启用的集群数一致
4. 检查 Worker Lambda 的 CloudWatch Logs 确认各集群日志采集正常
5. 检查 S3 桶中 `audit-logs/{cluster_id}/` 路径下是否有新文件

---

## 资源清理

如需删除所有资源，按以下顺序操作：

1. Athena > Saved queries > 删除 7 个 `Audit Logs -` 开头的查询
2. AWS Glue > Tables > 删除 `audit_logs`
3. AWS Glue > Databases > 删除 `rds_audit_logs`
4. EventBridge > 规则 > 删除 `rds-audit-cluster-discovery-schedule`
5. EventBridge > 规则 > 删除 `rds-audit-dispatcher-schedule`
6. SQS > 队列 > 删除 `rds-audit-dispatcher-dlq`
7. Lambda > 函数 > 删除 `rds-audit-cluster-discovery`
8. Lambda > 函数 > 删除 `rds-audit-log-dispatcher`
9. Lambda > 函数 > 删除 `rds-audit-log-retriever`
10. CloudWatch > 告警 > 删除 `rds-audit-worker-errors`
11. CloudWatch > 告警 > 删除 `rds-audit-worker-duration-p99`
12. IAM > 角色 > 删除 `rds-audit-discovery-role`
13. IAM > 角色 > 删除 `rds-audit-dispatcher-role`
14. IAM > 角色 > 删除 `rds-audit-worker-role`
15. DynamoDB > 表 > 删除 `rds-audit-log-state`
