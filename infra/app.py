#!/usr/bin/env python3
import aws_cdk as cdk
from stacks.screener_stack import ScreenerInfraStack

app = cdk.App()
ScreenerInfraStack(app, "ScreenerInfraStack",
    env=cdk.Environment(region="eu-central-1")
)
app.synth()
