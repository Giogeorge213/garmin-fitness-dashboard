"""
Architecture diagram (as code) for the Garmin Fitness Dashboard.

Renders `architecture.png` with real AWS icons. Diagram-as-code, so it lives next
to the CDK and stays in sync with the real deployment.

Run:
    sudo apt-get install -y graphviz python3-venv     # graphviz binary is required
    python3 -m venv ~/.venvs/diagrams
    ~/.venvs/diagrams/bin/pip install diagrams
    ~/.venvs/diagrams/bin/python architecture.py      # -> architecture.png
"""
from diagrams import Diagram, Cluster, Edge
from diagrams.aws.compute import Fargate, Lambda, EC2ContainerRegistry
from diagrams.aws.storage import S3
from diagrams.aws.network import CloudFront, APIGateway
from diagrams.aws.integration import Eventbridge, SimpleNotificationServiceSns
from diagrams.aws.database import Dynamodb
from diagrams.aws.analytics import Athena
from diagrams.aws.general import User
from diagrams.onprem.client import Client

# Bedrock node was added in newer `diagrams` releases; fall back gracefully.
try:
    from diagrams.aws.ml import Bedrock
except ImportError:
    from diagrams.aws.general import General as Bedrock

graph_attr = {"fontsize": "18", "labelloc": "t", "pad": "0.5", "splines": "spline"}

with Diagram(
    "Garmin Fitness Dashboard - Serverless + Self-Refreshing",
    filename="architecture",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
):
    visitor = User("Visitor")
    garmin = Client("Garmin Connect API")

    with Cluster("Weekly Refresh (no machine needed)"):
        schedule = Eventbridge("EventBridge\n(weekly, Sun 13:00 UTC)")
        ecr = EC2ContainerRegistry("ECR\n(container image)")
        task = Fargate("Fargate task\npull -> render -> publish")
        schedule >> Edge(label="triggers") >> task
        ecr >> Edge(style="dashed", label="image") >> task

    with Cluster("Data & State (S3 + Athena)"):
        state = S3("State bucket\ntoken + raw cache")
        parquet = S3("Parquet\nactivities / wellness")
        athena = Athena("Athena\ngarmin.*")
        parquet >> Edge(style="dashed") >> athena

    with Cluster("Static Site"):
        site = S3("Site bucket\n(private, OAC)")
        cdn = CloudFront("CloudFront")
        site >> cdn

    with Cluster("Ask-the-data Chat"):
        api = APIGateway("HTTP API\n/ask (throttled)")
        chat = Lambda("Chat Lambda")
        model = Bedrock("Bedrock\n(Nova)")
        ddb = Dynamodb("DynamoDB\nrate limits")
        sns = SimpleNotificationServiceSns("SNS\ncap alert")
        api >> chat >> model
        chat >> ddb
        chat >> sns

    # ---- Refresh flow ----
    garmin >> Edge(label="pull (incremental)") >> task
    task >> Edge(label="restore/persist") >> state
    task >> Edge(label="write parquet") >> parquet
    task >> Edge(label="sync site") >> site
    task >> Edge(label="invalidate") >> cdn

    # ---- Serving + chat flow ----
    visitor >> Edge(label="HTTPS") >> cdn
    visitor >> Edge(label="question") >> api
    chat >> Edge(style="dashed", label="read-only SQL") >> athena
