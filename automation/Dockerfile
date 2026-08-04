# Weekly Garmin refresh image, run as a scheduled AWS Fargate task.
#
# BUILD CONTEXT IS THE REPO ROOT (so it can COPY pipeline/):
#   docker build -f automation/Dockerfile -t garmin-refresh .
#   (or: finch build -f automation/Dockerfile -t garmin-refresh .)
#
# The CDK stack builds + pushes this to ECR and wires it to an EventBridge
# weekly schedule; AWS auth at runtime comes from the Fargate task role.
FROM python:3.12-slim

COPY automation/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app

# The whole pipeline: wellness/routes/render (pipeline/) + vendored activities
# (pipeline/activities/) + manual_records.json. render.py generates site/.
COPY pipeline/ ./pipeline/
COPY automation/run_refresh.sh ./run_refresh.sh
RUN chmod +x ./run_refresh.sh

ENTRYPOINT ["./run_refresh.sh"]
