#!/usr/bin/env python3
"""Aurora/RDS Audit Log Solution Deploy - CDK App Entry Point"""
import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks
from stack import RDSAuditSolutionStack

app = cdk.App()
cdk.Aspects.of(app).add(AwsSolutionsChecks())

# Environment configuration
env = cdk.Environment(
    region=app.node.try_get_context("region") or "us-west-1"
)

RDSAuditSolutionStack(app, "RDSAuditSolutionStack",
    env=env,
    description="Aurora/RDS MySQL audit log solution"
)

app.synth()
