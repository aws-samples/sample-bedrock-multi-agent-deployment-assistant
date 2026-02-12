#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { AwsSolutionsChecks } from "cdk-nag";
import { AiLcmStack } from "../lib/ai-lcm-stack";

const app = new cdk.App();

new AiLcmStack(app, "AiLcmStack", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION || "us-east-1",
  },
  description: "AI-LCM Platform infrastructure",
});

// Enable cdk-nag AWS Solutions checks on all stacks
cdk.Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));
