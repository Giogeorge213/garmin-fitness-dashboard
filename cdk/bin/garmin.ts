#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { GarminDashboardStack } from '../lib/garmin-dashboard-stack';

const app = new cdk.App();

// Personal account (profile "personal"). Override via context if needed:
//   cdk deploy -c budgetEmail=you@example.com -c budgetUsd=20 --profile personal
new GarminDashboardStack(app, 'GarminDashboard', {
  env: { account: '989567198465', region: 'us-east-1' },
  // Amazon Nova Lite: Amazon-owned (no AWS Marketplace subscription needed),
  // cheap, invoked via the model-agnostic Converse API. Anthropic models on
  // Bedrock require a Marketplace subscription completed from the console.
  modelId: app.node.tryGetContext('modelId') ?? 'amazon.nova-lite-v1:0',
  budgetLimitUsd: Number(app.node.tryGetContext('budgetUsd') ?? 20),
  notifyEmail: app.node.tryGetContext('notifyEmail') ?? app.node.tryGetContext('budgetEmail') ?? 'REPLACE_WITH_YOUR_EMAIL',
  ipDailyMax: Number(app.node.tryGetContext('ipDailyMax') ?? 25),
  globalDailyMax: Number(app.node.tryGetContext('globalDailyMax') ?? 500),
});
