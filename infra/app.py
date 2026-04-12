#!/usr/bin/env python3
import aws_cdk as cdk
from stacks.screener_stack import ScreenerInfraStack
from stacks.publisher_stack import PublisherStack

app = cdk.App()

ENV = cdk.Environment(region="eu-central-1")

ScreenerInfraStack(app, "ScreenerInfraStack", env=ENV)

PublisherStack(app, "PublisherStack",
    env=ENV,
    event_bus_name="finviz-events",
)

app.synth()
