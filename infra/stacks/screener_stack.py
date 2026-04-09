from aws_cdk import (
    Stack,
    RemovalPolicy,
    CfnOutput,
    aws_s3 as s3,
    aws_iam as iam,
)
from constructs import Construct


class ScreenerInfraStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # S3 Bucket
        bucket = s3.Bucket(
            self,
            "ScreenerDataBucket",
            bucket_name="screener-data-repository",
            versioned=False,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # IAM User
        bot_user = iam.User(
            self,
            "FinvizScreenerBot",
            user_name="finviz-screener-bot",
        )

        # IAM Policy scoped to the bucket
        bot_policy = iam.Policy(
            self,
            "FinvizScreenerBotPolicy",
            statements=[
                iam.PolicyStatement(
                    actions=[
                        "s3:PutObject",
                        "s3:GetObject",
                        "s3:ListBucket",
                    ],
                    resources=[
                        bucket.bucket_arn,
                        bucket.bucket_arn + "/*",
                    ],
                )
            ],
        )

        # Attach policy to user
        bot_user.attach_inline_policy(bot_policy)

        # Access key for the user
        access_key = iam.CfnAccessKey(
            self,
            "FinvizScreenerBotAccessKey",
            user_name=bot_user.user_name,
        )

        # Outputs
        CfnOutput(
            self,
            "BotAccessKeyId",
            value=access_key.ref,
            description="Access Key ID for finviz-screener-bot",
        )

        CfnOutput(
            self,
            "BotSecretAccessKey",
            value=access_key.attr_secret_access_key,
            description="Secret Access Key for finviz-screener-bot",
        )
