from aws_cdk import (
    Stack,
    Duration,
    aws_events as events,
    aws_events_targets as targets,
    aws_lambda as lambda_,
    aws_iam as iam,
)
from constructs import Construct

SSM_PREFIX = "/anva-trade"


class PublisherStack(Stack):
    def __init__(self, scope: Construct, construct_id: str,
                 event_bus_name: str = "finviz-events", **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        # ── 1. Custom EventBus ───────────────────────────────────────────
        bus = events.EventBus(
            self, "FinvizEventBus",
            event_bus_name=event_bus_name,
        )

        # ── 2. Single X Publisher Lambda — reads SSM creds at runtime ────
        # Dependencies are pre-installed into the asset dir via:
        #   pip install -r lambdas/x_publisher/requirements.txt -t lambdas/x_publisher/
        x_fn = lambda_.Function(
            self, "XPublisher",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="x_publisher.handler",
            code=lambda_.Code.from_asset("lambdas/x_publisher"),
            timeout=Duration.seconds(30),
        )

        # Grant Lambda runtime access to SecureString params
        x_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameters"],
            resources=[
                f"arn:aws:ssm:{self.region}:{self.account}:parameter{SSM_PREFIX}/*"
            ],
        ))

        # ── 3. One rule per event type ───────────────────────────────────
        # MarketDailySummary rule is wired but XPublisher returns "skipped" for it.
        # Reserved for future SlackPublisher / DiscordPublisher subscribers.
        # TODO: When adding new channel Lambdas, add them as targets on MarketDailySummaryRule.
        for detail_type, rule_id in [
            ("MarketDailySummary", "MarketDailySummaryRule"),
            ("ScreenerCompleted",  "ScreenerRule"),
            ("PersistencePick",    "PersistenceRule"),
        ]:
            events.Rule(
                self, rule_id,
                event_bus=bus,
                event_pattern=events.EventPattern(
                    source=["finviz.screener"],
                    detail_type=[detail_type],
                ),
                targets=[targets.LambdaFunction(x_fn)],
            )

        # ── 4. IAM: finviz-screener-bot (existing GHA user) can PutEvents ─
        bot_user = iam.User.from_user_name(
            self, "ScreenerBotUser", "finviz-screener-bot"
        )
        bus.grant_put_events_to(bot_user)
