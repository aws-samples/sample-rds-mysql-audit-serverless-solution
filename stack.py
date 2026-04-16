"""
Aurora/RDS Audit Solution Stack — Multi-Cluster Edition

Deploys:
  - Worker Lambda (ARM64, Python 3.12) to retrieve RDS audit logs
  - Dispatcher Lambda to read cluster config from S3 and trigger Worker per cluster
  - EventBridge rule for scheduled Dispatcher invocation
  - DynamoDB table for state tracking
  - IAM roles with least-privilege permissions
"""
from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_athena as athena,
    aws_cloudwatch as cloudwatch,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_glue as glue,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_sqs as sqs,
)
from constructs import Construct
from cdk_nag import NagSuppressions


class RDSAuditSolutionStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── Context parameters ────────────────────────────────────────────
        instance_ids = self.node.try_get_context("aurora_instance_ids") or ""
        s3_bucket_name = self.node.try_get_context("s3_bucket_name")
        lambda_memory = int(self.node.try_get_context("lambda_memory_mb") or 512)
        lambda_timeout = int(self.node.try_get_context("lambda_timeout_seconds") or 300)

        # Dispatcher / multi-cluster parameters
        config_s3_key = self.node.try_get_context("config_s3_key") or "config/cluster_config.json"
        dispatcher_schedule_minutes = int(
            self.node.try_get_context("dispatcher_schedule_minutes") or 5
        )
        dispatcher_memory = int(self.node.try_get_context("dispatcher_memory_mb") or 256)
        dispatcher_timeout = int(self.node.try_get_context("dispatcher_timeout_seconds") or 60)
        discovery_memory = int(self.node.try_get_context("discovery_memory_mb") or 256)
        discovery_timeout = int(self.node.try_get_context("discovery_timeout_seconds") or 120)

        # Glue / Athena parameters
        glue_database_name = self.node.try_get_context("glue_database_name") or "rds_audit_logs"
        glue_table_name = self.node.try_get_context("glue_table_name") or "audit_logs"

        if not s3_bucket_name or s3_bucket_name == "your-audit-logs-bucket":
            raise ValueError(
                "请在 cdk.context.json 中设置 s3_bucket_name（S3 桶名）"
            )

        lambda_code_path = str(Path(__file__).parent / "lambda_code")

        # ── DynamoDB state table ──────────────────────────────────────────
        state_table = dynamodb.Table(
            self, "AuditLogStateTable",
            table_name="rds-audit-log-state",
            partition_key=dynamodb.Attribute(
                name="log_file_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
            time_to_live_attribute="expiration_time",
        )

        # ── Worker Lambda ─────────────────────────────────────────────────
        worker_log_group = logs.LogGroup(
            self, "WorkerLogGroup",
            log_group_name="/aws/lambda/rds-audit-log-retriever",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.RETAIN,
        )

        worker_env = {
            "BUCKET_NAME": s3_bucket_name,
            "STATE_TABLE_NAME": state_table.table_name,
            "LOOKBACK_MINUTES": str(dispatcher_schedule_minutes * 2),
        }
        # Backward compatibility: if aurora_instance_ids is set, keep INSTANCE_IDS env var
        if instance_ids and instance_ids != "your-cluster-instance-1,your-cluster-instance-2":
            worker_env["INSTANCE_IDS"] = instance_ids

        worker_fn = _lambda.Function(
            self, "AuditLogRetriever",
            function_name="rds-audit-log-retriever",
            runtime=_lambda.Runtime.PYTHON_3_14,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.lambda_handler",
            code=_lambda.Code.from_asset(lambda_code_path),
            memory_size=lambda_memory,
            timeout=Duration.seconds(lambda_timeout),
            environment=worker_env,
            log_group=worker_log_group,
        )

        # ── Worker IAM permissions ────────────────────────────────────────

        # RDS: wildcard ARN (instance list is dynamic in multi-cluster mode)
        worker_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "rds:DescribeDBLogFiles",
                "rds:DownloadDBLogFilePortion",
            ],
            resources=[f"arn:aws:rds:{self.region}:{self.account}:db:*"],
        ))

        # S3: write to target bucket
        worker_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "s3:PutObject",
                "s3:AbortMultipartUpload",
            ],
            resources=[f"arn:aws:s3:::{s3_bucket_name}/audit-logs/*"],
        ))

        # DynamoDB: read/write state table
        state_table.grant_read_write_data(worker_fn)

        # ── CloudWatch Alarms ─────────────────────────────────────────────
        cloudwatch.Alarm(
            self, "WorkerErrorAlarm",
            alarm_name="rds-audit-worker-errors",
            alarm_description="Worker Lambda invocation errors",
            metric=worker_fn.metric_errors(
                period=Duration.minutes(5),
                statistic="Sum",
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        cloudwatch.Alarm(
            self, "WorkerDurationAlarm",
            alarm_name="rds-audit-worker-duration-p99",
            alarm_description="Worker Lambda p99 duration exceeds 80% of timeout",
            metric=worker_fn.metric_duration(
                period=Duration.minutes(5),
                statistic="p99",
            ),
            threshold=lambda_timeout * 0.8 * 1000,  # milliseconds
            evaluation_periods=3,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        # ── Dispatcher Lambda ─────────────────────────────────────────────
        dispatcher_log_group = logs.LogGroup(
            self, "DispatcherLogGroup",
            log_group_name="/aws/lambda/rds-audit-log-dispatcher",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.RETAIN,
        )

        dispatcher_fn = _lambda.Function(
            self, "AuditLogDispatcher",
            function_name="rds-audit-log-dispatcher",
            runtime=_lambda.Runtime.PYTHON_3_14,
            architecture=_lambda.Architecture.ARM_64,
            handler="dispatcher.lambda_handler",
            code=_lambda.Code.from_asset(lambda_code_path),
            memory_size=dispatcher_memory,
            timeout=Duration.seconds(dispatcher_timeout),
            environment={
                "CONFIG_BUCKET": s3_bucket_name,
                "CONFIG_S3_KEY": config_s3_key,
                "WORKER_FUNCTION_NAME": worker_fn.function_name,
            },
            log_group=dispatcher_log_group,
        )

        # ── Dispatcher IAM permissions ────────────────────────────────────

        # S3: read cluster config file
        dispatcher_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[f"arn:aws:s3:::{s3_bucket_name}/{config_s3_key}"],
        ))

        # Lambda: async invoke Worker
        worker_fn.grant_invoke(dispatcher_fn)

        # ── EventBridge schedule (triggers Dispatcher, not Worker) ────────
        rule = events.Rule(
            self, "DispatcherSchedule",
            rule_name="rds-audit-dispatcher-schedule",
            schedule=events.Schedule.rate(Duration.minutes(dispatcher_schedule_minutes)),
            description=f"Trigger audit log dispatcher every {dispatcher_schedule_minutes} minutes",
        )
        dispatcher_dlq = sqs.Queue(self, "DispatcherDLQ", enforce_ssl=True)
        rule.add_target(targets.LambdaFunction(
            dispatcher_fn,
            dead_letter_queue=dispatcher_dlq,
            max_event_age=Duration.minutes(5),
            retry_attempts=2,
        ))

        # ── Cluster Discovery Lambda ──────────────────────────────────────
        target_rds_type = self.node.try_get_context("target_rds_type") or "aurora-mysql"

        discovery_log_group = logs.LogGroup(
            self, "DiscoveryLogGroup",
            log_group_name="/aws/lambda/rds-audit-cluster-discovery",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.RETAIN,
        )

        discovery_fn = _lambda.Function(
            self, "ClusterDiscovery",
            function_name="rds-audit-cluster-discovery",
            runtime=_lambda.Runtime.PYTHON_3_14,
            architecture=_lambda.Architecture.ARM_64,
            handler="cluster_discovery.lambda_handler",
            code=_lambda.Code.from_asset(lambda_code_path),
            memory_size=discovery_memory,
            timeout=Duration.seconds(discovery_timeout),
            environment={
                "CONFIG_BUCKET": s3_bucket_name,
                "CONFIG_S3_KEY": config_s3_key,
                "TARGET_RDS_TYPE": target_rds_type,
            },
            log_group=discovery_log_group,
        )

        # Discovery IAM: RDS describe clusters + instances + parameter/option groups
        discovery_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "rds:DescribeDBClusters",
                "rds:DescribeDBClusterParameterGroups",
                "rds:DescribeDBClusterParameters",
                "rds:DescribeDBInstances",
                "rds:DescribeOptionGroups",
            ],
            resources=[
                f"arn:aws:rds:{self.region}:{self.account}:cluster:*",
                f"arn:aws:rds:{self.region}:{self.account}:cluster-pg:*",
                f"arn:aws:rds:{self.region}:{self.account}:og:*",
                f"arn:aws:rds:{self.region}:{self.account}:db:*",
            ],
        ))

        # Discovery IAM: S3 read + write config file
        discovery_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject", "s3:PutObject"],
            resources=[f"arn:aws:s3:::{s3_bucket_name}/{config_s3_key}"],
        ))

        # Daily schedule for cluster discovery
        discovery_rule = events.Rule(
            self, "DiscoverySchedule",
            rule_name="rds-audit-cluster-discovery-schedule",
            schedule=events.Schedule.cron(hour="2", minute="0"),
            description="Daily cluster discovery at 02:00 UTC",
        )
        discovery_rule.add_target(targets.LambdaFunction(discovery_fn))

        # ── Outputs ───────────────────────────────────────────────────────
        cdk.CfnOutput(self, "WorkerFunctionName", value=worker_fn.function_name)
        cdk.CfnOutput(self, "DispatcherFunctionName", value=dispatcher_fn.function_name)
        cdk.CfnOutput(self, "DiscoveryFunctionName", value=discovery_fn.function_name)
        cdk.CfnOutput(self, "StateTableName", value=state_table.table_name)
        cdk.CfnOutput(self, "ScheduleRuleName", value=rule.rule_name)
        cdk.CfnOutput(self, "DiscoveryScheduleRuleName", value=discovery_rule.rule_name)
        cdk.CfnOutput(self, "S3BucketName", value=s3_bucket_name)
        cdk.CfnOutput(self, "ConfigS3Key", value=config_s3_key)

        # ── Glue Database ─────────────────────────────────────────────────
        glue_db = glue.CfnDatabase(
            self, "AuditGlueDatabase",
            catalog_id=self.account,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name=glue_database_name,
                description="Aurora/RDS MySQL audit logs database for Athena queries",
            ),
        )

        # ── Glue Table ────────────────────────────────────────────────────
        columns = [
            glue.CfnTable.ColumnProperty(name="timestamp", type="string"),
            glue.CfnTable.ColumnProperty(name="serverhost", type="string"),
            glue.CfnTable.ColumnProperty(name="username", type="string"),
            glue.CfnTable.ColumnProperty(name="host", type="string"),
            glue.CfnTable.ColumnProperty(name="connectionid", type="string"),
            glue.CfnTable.ColumnProperty(name="queryid", type="string"),
            glue.CfnTable.ColumnProperty(name="operation", type="string"),
            glue.CfnTable.ColumnProperty(name="database", type="string"),
            glue.CfnTable.ColumnProperty(name="object", type="string"),
            glue.CfnTable.ColumnProperty(name="retcode", type="string"),
        ]

        glue_table = glue.CfnTable(
            self, "AuditGlueTable",
            catalog_id=self.account,
            database_name=glue_database_name,
            table_input=glue.CfnTable.TableInputProperty(
                name=glue_table_name,
                description="Aurora/RDS MySQL audit log table",
                table_type="EXTERNAL_TABLE",
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    columns=columns,
                    location=f"s3://{s3_bucket_name}/audit-logs/",
                    input_format="org.apache.hadoop.mapred.TextInputFormat",
                    output_format="org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library="org.apache.hadoop.hive.serde2.OpenCSVSerde",
                        parameters={
                            "separatorChar": ",",
                            "quoteChar": "'",
                            "escapeChar": "\\",
                        },
                    ),
                ),
            ),
        )
        glue_table.add_dependency(glue_db)

        # ── Athena Saved Queries ──────────────────────────────────────────
        db_tbl = f"{glue_database_name}.{glue_table_name}"

        # event_time expression: handles both Aurora (microsecond Unix ts)
        # and RDS MySQL (YYYYMMDD HH:MM:SS) timestamp formats
        event_time_expr = (
            "CASE WHEN regexp_like(timestamp, '^\\d+$')\n"
            "            THEN from_unixtime(CAST(timestamp AS BIGINT) / 1000000)\n"
            "            ELSE TRY(parse_datetime(timestamp, 'yyyyMMdd HH:mm:ss'))\n"
            "       END AS event_time"
        )

        # Common SELECT columns used by most queries
        select_cols = (
            f"SELECT {event_time_expr},\n"
            f"       serverhost, username, host,\n"
            f"       TRY_CAST(connectionid AS BIGINT) AS connectionid,\n"
            f"       TRY_CAST(queryid AS BIGINT) AS queryid,\n"
            f"       operation, database, object,\n"
            f"       TRY_CAST(retcode AS INTEGER) AS retcode\n"
        )

        saved_queries = {
            "QueryByUser": {
                "name": "Audit Logs - Query By User",
                "description": "Query audit logs filtered by a specific username",
                "sql": (
                    f"-- Query audit logs by username\n"
                    f"-- Replace 'target_user' with the actual username\n"
                    f"{select_cols}"
                    f"FROM {db_tbl}\n"
                    f"WHERE username = 'target_user'\n"
                    f"ORDER BY timestamp DESC\n"
                    f"LIMIT 100;\n"
                ),
            },
            "QueryByTimeRange": {
                "name": "Audit Logs - Query By Time Range",
                "description": "Query audit logs within a specific time range",
                "sql": (
                    f"-- Query audit logs within a time range\n"
                    f"-- Supports both Aurora (microsecond Unix ts) and RDS MySQL (YYYYMMDD HH:MM:SS)\n"
                    f"{select_cols}"
                    f"FROM {db_tbl}\n"
                    f"WHERE CASE WHEN regexp_like(timestamp, '^\\d+$')\n"
                    f"           THEN from_unixtime(CAST(timestamp AS BIGINT) / 1000000)\n"
                    f"           ELSE TRY(parse_datetime(timestamp, 'yyyyMMdd HH:mm:ss'))\n"
                    f"      END >= TIMESTAMP '2026-03-01 00:00:00'\n"
                    f"  AND CASE WHEN regexp_like(timestamp, '^\\d+$')\n"
                    f"           THEN from_unixtime(CAST(timestamp AS BIGINT) / 1000000)\n"
                    f"           ELSE TRY(parse_datetime(timestamp, 'yyyyMMdd HH:mm:ss'))\n"
                    f"      END <= TIMESTAMP '2026-12-31 23:59:59'\n"
                    f"ORDER BY timestamp DESC\n"
                    f"LIMIT 100;\n"
                ),
            },
            "QueryFailedOps": {
                "name": "Audit Logs - Failed Operations",
                "description": "Query operations that failed (retcode != 0)",
                "sql": (
                    f"-- Query failed operations (retcode != 0)\n"
                    f"{select_cols}"
                    f"FROM {db_tbl}\n"
                    f"WHERE TRY_CAST(retcode AS INTEGER) != 0\n"
                    f"ORDER BY timestamp DESC\n"
                    f"LIMIT 100;\n"
                ),
            },
            "QueryDDLOps": {
                "name": "Audit Logs - DDL Operations",
                "description": "Query DDL operations (CREATE, ALTER, DROP, TRUNCATE)",
                "sql": (
                    f"-- Query DDL operations\n"
                    f"{select_cols}"
                    f"FROM {db_tbl}\n"
                    f"WHERE operation IN ('CREATE', 'ALTER', 'DROP', 'TRUNCATE')\n"
                    f"ORDER BY timestamp DESC\n"
                    f"LIMIT 100;\n"
                ),
            },
            "QueryDMLOps": {
                "name": "Audit Logs - DML Operations",
                "description": "Query DML operations (INSERT, UPDATE, DELETE)",
                "sql": (
                    f"-- Query DML operations\n"
                    f"{select_cols}"
                    f"FROM {db_tbl}\n"
                    f"WHERE operation IN ('INSERT', 'UPDATE', 'DELETE')\n"
                    f"ORDER BY timestamp DESC\n"
                    f"LIMIT 100;\n"
                ),
            },
            "QueryUserStats": {
                "name": "Audit Logs - User Operation Statistics",
                "description": "Statistics of operations grouped by username and operation type",
                "sql": (
                    f"-- Statistics of operations per user\n"
                    f"SELECT username,\n"
                    f"       operation,\n"
                    f"       COUNT(*) AS operation_count,\n"
                    f"       SUM(CASE WHEN TRY_CAST(retcode AS INTEGER) != 0 THEN 1 ELSE 0 END) AS failed_count\n"
                    f"FROM {db_tbl}\n"
                    f"GROUP BY username, operation\n"
                    f"ORDER BY operation_count DESC;\n"
                ),
            },
            "QueryDCLOps": {
                "name": "Audit Logs - DCL Operations",
                "description": "Query DCL operations (GRANT, REVOKE)",
                "sql": (
                    f"-- Query DCL operations\n"
                    f"{select_cols}"
                    f"FROM {db_tbl}\n"
                    f"WHERE operation IN ('GRANT', 'REVOKE')\n"
                    f"ORDER BY timestamp DESC\n"
                    f"LIMIT 100;\n"
                ),
            },
        }

        for resource_id, q in saved_queries.items():
            athena.CfnNamedQuery(
                self, resource_id,
                database=glue_database_name,
                query_string=q["sql"],
                name=q["name"],
                description=q["description"],
            )

        cdk.CfnOutput(self, "GlueDatabaseName", value=glue_database_name)
        cdk.CfnOutput(self, "GlueTableName", value=glue_table_name)

        # ── cdk-nag Suppressions ──────────────────────────────────────────
        NagSuppressions.add_resource_suppressions(
            dispatcher_dlq,
            [{"id": "AwsSolutions-SQS3", "reason": "This queue IS the DLQ for EventBridge target; a nested DLQ is unnecessary."}],
        )
        for fn in [worker_fn, dispatcher_fn, discovery_fn]:
            NagSuppressions.add_resource_suppressions(
                fn,
                [{"id": "AwsSolutions-IAM4", "reason": "AWSLambdaBasicExecutionRole is the standard managed policy auto-attached by CDK for CloudWatch Logs access."},
                 {"id": "AwsSolutions-IAM5", "reason": "Wildcard resources required: RDS instance/cluster IDs are dynamic (auto-discovered), S3 audit-log paths include date partitions, and Lambda invoke ARN requires :* suffix."}],
                apply_to_children=True,
            )
