import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigw from 'aws-cdk-lib/aws-apigatewayv2';
import { HttpLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ddb from 'aws-cdk-lib/aws-dynamodb';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as subs from 'aws-cdk-lib/aws-sns-subscriptions';
import * as budgets from 'aws-cdk-lib/aws-budgets';
import * as glue from 'aws-cdk-lib/aws-glue';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as path from 'path';

export interface GarminDashboardStackProps extends cdk.StackProps {
  /** Bedrock model id for the chat box (must have model access enabled). */
  modelId: string;
  /** Monthly cost budget in USD; email alert fires at 80%. */
  budgetLimitUsd: number;
  /** Email for both the monthly budget alert and the daily-cap SNS alert. */
  notifyEmail: string;
  /** Max chat questions per client IP per UTC day. */
  ipDailyMax: number;
  /** Max chat questions across ALL users per UTC day (hard spend brake). */
  globalDailyMax: number;
}

/**
 * Garmin fitness dashboard — hosting + rate-limited chat.
 *
 * CDK OWNS (infra only):
 *   - private S3 bucket (site content synced by the local weekly job, NOT this stack)
 *   - CloudFront distribution (OAC -> the private bucket)
 *   - chat Lambda (Python) -> Bedrock InvokeModel, with per-IP + global daily caps
 *   - DynamoDB counter table (per-IP + global daily question counters, TTL-expired)
 *   - HTTP API (POST /ask), throttled, CORS to the CF domain
 *   - SNS topic + email sub: fires the instant the GLOBAL daily cap is hit
 *   - monthly cost budget + email alert (backstop)
 */
export class GarminDashboardStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: GarminDashboardStackProps) {
    super(scope, id, props);

    // ---- Private site bucket (CloudFront reads via OAC) ----
    const bucket = new s3.Bucket(this, 'SiteBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // ---- CloudFront (OAC -> private bucket) ----
    const distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: 'Garmin fitness dashboard',
      defaultRootObject: 'index.html',
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(bucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      },
      errorResponses: [
        { httpStatus: 403, responseHttpStatus: 200, responsePagePath: '/index.html' },
        { httpStatus: 404, responseHttpStatus: 200, responsePagePath: '/index.html' },
      ],
    });

    // ---- Rate-limit counter table (per-IP + global, per UTC day) ----
    // pk = "ip#<ip>#<yyyy-mm-dd>" or "global#<yyyy-mm-dd>"; rows auto-expire via TTL.
    const counters = new ddb.Table(this, 'RateCounters', {
      partitionKey: { name: 'pk', type: ddb.AttributeType.STRING },
      billingMode: ddb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ---- Alert topic: fires when the GLOBAL daily cap is hit ----
    const alertTopic = new sns.Topic(this, 'DailyCapAlert', {
      displayName: 'Garmin dashboard daily cap alert',
    });
    alertTopic.addSubscription(new subs.EmailSubscription(props.notifyEmail));

    // ---- Chat Lambda (Bedrock + rate limiting) ----
    const chatFn = new lambda.Function(this, 'ChatFn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'chat')),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: {
        BUCKET_NAME: bucket.bucketName,
        DATA_KEY: 'data.json',
        MODEL_ID: props.modelId,
        COUNTER_TABLE: counters.tableName,
        IP_DAILY_MAX: String(props.ipDailyMax),
        GLOBAL_DAILY_MAX: String(props.globalDailyMax),
        ALERT_TOPIC_ARN: alertTopic.topicArn,
        ATHENA_DB: 'garmin',
        ATHENA_OUTPUT: 's3://strava-analytics-989567198465/athena-results/',
      },
    });
    bucket.grantRead(chatFn);
    counters.grantReadWriteData(chatFn);
    alertTopic.grantPublish(chatFn);
    chatFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel'],
      resources: ['*'],
    }));

    // Read-only SQL tool over the garmin.activities Athena table.
    const ANALYTICS_BUCKET = 'strava-analytics-989567198465';
    chatFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['athena:StartQueryExecution', 'athena:GetQueryExecution',
                'athena:GetQueryResults', 'athena:StopQueryExecution'],
      resources: [`arn:aws:athena:${this.region}:${this.account}:workgroup/primary`],
    }));
    chatFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['glue:GetTable', 'glue:GetDatabase', 'glue:GetPartitions'],
      resources: [
        `arn:aws:glue:${this.region}:${this.account}:catalog`,
        `arn:aws:glue:${this.region}:${this.account}:database/garmin`,
        `arn:aws:glue:${this.region}:${this.account}:table/garmin/activities`,
        `arn:aws:glue:${this.region}:${this.account}:table/garmin/wellness`,
      ],
    }));
    chatFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject', 's3:GetBucketLocation', 's3:ListBucket', 's3:PutObject'],
      resources: [`arn:aws:s3:::${ANALYTICS_BUCKET}`, `arn:aws:s3:::${ANALYTICS_BUCKET}/*`],
    }));

    // ---- HTTP API (POST /ask), throttled + CORS to the CF domain ----
    const httpApi = new apigw.HttpApi(this, 'ChatApi', {
      corsPreflight: {
        allowOrigins: [`https://${distribution.distributionDomainName}`],
        allowMethods: [apigw.CorsHttpMethod.POST],
        allowHeaders: ['content-type'],
      },
    });
    httpApi.addRoutes({
      path: '/ask',
      methods: [apigw.HttpMethod.POST],
      integration: new HttpLambdaIntegration('ChatIntegration', chatFn),
    });
    const stage = httpApi.defaultStage!.node.defaultChild as apigw.CfnStage;
    stage.defaultRouteSettings = { throttlingBurstLimit: 5, throttlingRateLimit: 2 };

    // ---- Monthly cost budget + email alert at 80% (backstop) ----
    new budgets.CfnBudget(this, 'MonthlyBudget', {
      budget: {
        budgetType: 'COST',
        timeUnit: 'MONTHLY',
        budgetLimit: { amount: props.budgetLimitUsd, unit: 'USD' },
      },
      notificationsWithSubscribers: [{
        notification: { notificationType: 'ACTUAL', comparisonOperator: 'GREATER_THAN', threshold: 80 },
        subscribers: [{ subscriptionType: 'EMAIL', address: props.notifyEmail }],
      }],
    });

    // ---- Glue: 'garmin' database + activities/wellness tables (Athena) ----
    // These are EXTERNAL tables over parquet the refresh script uploads to the
    // analytics bucket. Schemas are pinned (the transforms write fixed dtypes),
    // so they're declared explicitly rather than crawled.
    const glueDb = new glue.CfnDatabase(this, 'GarminDb', {
      catalogId: this.account,
      databaseInput: { name: 'garmin' },
    });
    const parquetTable = (id: string, name: string, prefix: string,
                          columns: { name: string; type: string }[]) => {
      const t = new glue.CfnTable(this, id, {
        catalogId: this.account,
        databaseName: 'garmin',
        tableInput: {
          name,
          tableType: 'EXTERNAL_TABLE',
          parameters: { classification: 'parquet', EXTERNAL: 'TRUE' },
          storageDescriptor: {
            location: `s3://${ANALYTICS_BUCKET}/${prefix}/`,
            inputFormat: 'org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat',
            outputFormat: 'org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat',
            serdeInfo: { serializationLibrary: 'org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe' },
            columns,
          },
        },
      });
      t.addDependency(glueDb);
      return t;
    };
    parquetTable('ActivitiesTable', 'activities', 'garmin/curated', [
      { name: 'activity_id', type: 'bigint' }, { name: 'name', type: 'string' }, { name: 'sport', type: 'string' },
      { name: 'start_datetime_local', type: 'string' }, { name: 'date', type: 'string' }, { name: 'year', type: 'bigint' },
      { name: 'month', type: 'string' }, { name: 'week', type: 'string' }, { name: 'day_of_week', type: 'string' },
      { name: 'distance_mi', type: 'double' }, { name: 'moving_time_min', type: 'double' }, { name: 'total_time_min', type: 'double' },
      { name: 'pace_min_per_mi', type: 'double' }, { name: 'avg_speed_mph', type: 'double' }, { name: 'elevation_gain_ft', type: 'double' },
      { name: 'average_hr', type: 'double' }, { name: 'max_hr', type: 'double' }, { name: 'avg_cadence', type: 'double' },
      { name: 'calories', type: 'double' }, { name: 'latitude', type: 'double' }, { name: 'longitude', type: 'double' },
      { name: 'days_since_last_activity', type: 'bigint' },
    ]);
    parquetTable('WellnessTable', 'wellness', 'garmin/wellness', [
      { name: 'date', type: 'string' }, { name: 'steps', type: 'bigint' }, { name: 'distance_km', type: 'double' },
      { name: 'floors', type: 'double' }, { name: 'moderate_min', type: 'bigint' }, { name: 'vigorous_min', type: 'bigint' },
      { name: 'total_kcal', type: 'double' }, { name: 'active_kcal', type: 'double' }, { name: 'resting_hr', type: 'bigint' },
      { name: 'min_hr', type: 'bigint' }, { name: 'max_hr', type: 'bigint' }, { name: 'avg_stress', type: 'bigint' },
      { name: 'max_stress', type: 'bigint' }, { name: 'body_battery_charged', type: 'bigint' }, { name: 'body_battery_drained', type: 'bigint' },
      { name: 'sleep_hours', type: 'double' }, { name: 'deep_sleep_hours', type: 'double' }, { name: 'light_sleep_hours', type: 'double' },
      { name: 'rem_sleep_hours', type: 'double' }, { name: 'awake_hours', type: 'double' }, { name: 'hrv', type: 'bigint' },
      { name: 'hrv_status', type: 'string' }, { name: 'vo2max', type: 'double' }, { name: 'readiness_score', type: 'bigint' },
      { name: 'readiness_level', type: 'string' }, { name: 'weight_kg', type: 'double' }, { name: 'hydration_ml', type: 'bigint' },
    ]);

    // ---- Weekly refresh: Fargate scheduled task ---------------------------
    // Container (automation/Dockerfile) runs the pull -> transform -> render ->
    // publish flow weekly. Ephemeral: EventBridge starts the task, it runs a few
    // minutes, then exits. Only billed for the runtime (pennies/month).
    const ANALYTICS_BUCKET_ARN = `arn:aws:s3:::${ANALYTICS_BUCKET}`;

    // Minimal VPC: public subnets only, NO NAT gateway (NAT would add ~$32/mo).
    // The task gets a public IP so it can reach the Garmin API + S3 + CloudFront.
    const vpc = new ec2.Vpc(this, 'RefreshVpc', {
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [{ name: 'public', subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 }],
    });

    const cluster = new ecs.Cluster(this, 'RefreshCluster', { vpc });

    const taskDef = new ecs.FargateTaskDefinition(this, 'RefreshTask', {
      cpu: 1024,          // 1 vCPU
      memoryLimitMiB: 2048, // headroom for pandas/pyarrow + a first backfill
    });

    taskDef.addContainer('refresh', {
      // Build context = repo root (../.. from cdk/lib), Dockerfile in automation/.
      // Requires Docker/Finch locally at `cdk deploy` time to build + push to ECR.
      image: ecs.ContainerImage.fromAsset(path.join(__dirname, '..', '..'), {
        file: 'automation/Dockerfile',
        // CDK fingerprints the asset by walking the source tree, and that walk
        // does NOT honor .dockerignore — so without `exclude` it walks the full
        // ~1.1GB repo (cdk/node_modules, .git, cdk.out) over the slow /mnt/c
        // filesystem and the build never starts. `exclude` trims the walk itself.
        exclude: ['.git', 'cdk', 'cdk.out', 'pipeline/out', 'pipeline/**/out',
                  'site', 'docs', '**/__pycache__', '*.lnk'],
      }),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'garmin-refresh',
        logRetention: logs.RetentionDays.ONE_MONTH,
      }),
      environment: {
        SITE_BUCKET: bucket.bucketName,
        DISTRIBUTION_ID: distribution.distributionId,
        CHAT_API_URL: `${httpApi.apiEndpoint}/ask`,
        ANALYTICS_BUCKET,
      },
    });

    // Task role: site bucket (sync + --delete), analytics bucket (token / raw
    // cache / parquet read+write), and the CloudFront invalidation.
    bucket.grantReadWrite(taskDef.taskRole);
    taskDef.taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject', 's3:PutObject', 's3:DeleteObject',
                's3:ListBucket', 's3:GetBucketLocation'],
      resources: [ANALYTICS_BUCKET_ARN, `${ANALYTICS_BUCKET_ARN}/*`],
    }));
    taskDef.taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['cloudfront:CreateInvalidation'],
      resources: [`arn:aws:cloudfront::${this.account}:distribution/${distribution.distributionId}`],
    }));

    // Weekly: Sundays 13:00 UTC (~6 AM Pacific), matching the old local task.
    new events.Rule(this, 'RefreshSchedule', {
      schedule: events.Schedule.cron({ weekDay: 'SUN', hour: '13', minute: '0' }),
      targets: [new targets.EcsTask({
        cluster,
        taskDefinition: taskDef,
        taskCount: 1,
        assignPublicIp: true,
        subnetSelection: { subnetType: ec2.SubnetType.PUBLIC },
      })],
    });

    // ---- Outputs ----
    new cdk.CfnOutput(this, 'RefreshClusterName', { value: cluster.clusterName });
    new cdk.CfnOutput(this, 'RefreshTaskFamily', { value: taskDef.family });
    new cdk.CfnOutput(this, 'DashboardUrl', { value: `https://${distribution.distributionDomainName}` });
    new cdk.CfnOutput(this, 'ChatApiUrl', { value: `${httpApi.apiEndpoint}/ask` });
    new cdk.CfnOutput(this, 'BucketName', { value: bucket.bucketName });
    new cdk.CfnOutput(this, 'DistributionId', { value: distribution.distributionId });
  }
}
