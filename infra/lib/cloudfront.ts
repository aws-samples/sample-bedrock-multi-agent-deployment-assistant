import * as fs from "fs";
import * as cdk from "aws-cdk-lib";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
import * as path from "path";
import { NagSuppressions } from "cdk-nag";
import { Construct } from "constructs";

export interface CloudFrontConstructProps {
  /** Optional ACM certificate ARN (must be in us-east-1) for custom domain. */
  certificateArn?: string;
  /** Optional custom domain names (e.g., ["app.example.com"]). */
  domainNames?: string[];
  /** Optional S3 bucket for CloudFront access logs. */
  accessLogsBucket?: s3.IBucket;
}

/**
 * S3 + CloudFront distribution for the Next.js static frontend.
 *
 * Requires `output: 'export'` in next.config.ts to produce static HTML/JS/CSS.
 * CloudFront handles SPA routing via custom error responses (404 → /index.html).
 */
export class CloudFrontConstruct extends Construct {
  public readonly distribution: cloudfront.Distribution;
  public readonly siteBucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: CloudFrontConstructProps = {}) {
    super(scope, id);

    // S3 bucket for static site assets — private, accessed via CloudFront OAC
    this.siteBucket = new s3.Bucket(this, "SiteBucket", {
      bucketName: cdk.PhysicalName.GENERATE_IF_NEEDED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // CloudFront Origin Access Control for S3
    const oac = new cloudfront.S3OriginAccessControl(this, "OAC", {
      signing: cloudfront.Signing.SIGV4_ALWAYS,
    });

    // Security headers policy
    const responseHeadersPolicy = new cloudfront.ResponseHeadersPolicy(this, "SecurityHeaders", {
      responseHeadersPolicyName: "ai-deploy-frontend-security-headers",
      securityHeadersBehavior: {
        contentTypeOptions: { override: true },
        frameOptions: {
          frameOption: cloudfront.HeadersFrameOption.DENY,
          override: true,
        },
        referrerPolicy: {
          referrerPolicy: cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
          override: true,
        },
        strictTransportSecurity: {
          accessControlMaxAge: cdk.Duration.seconds(63072000),
          includeSubdomains: true,
          preload: true,
          override: true,
        },
      },
    });

    // CloudFront distribution
    const distributionProps: cloudfront.DistributionProps = {
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(this.siteBucket, {
          originAccessControl: oac,
        }),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        responseHeadersPolicy,
      },
      defaultRootObject: "index.html",
      enableLogging: !!props.accessLogsBucket,
      logBucket: props.accessLogsBucket as s3.Bucket | undefined,
      logFilePrefix: props.accessLogsBucket ? "cloudfront/" : undefined,
      // SPA fallback: serve index.html for 403/404 so client-side routing works
      errorResponses: [
        {
          httpStatus: 403,
          responseHttpStatus: 200,
          responsePagePath: "/index.html",
          ttl: cdk.Duration.seconds(0),
        },
        {
          httpStatus: 404,
          responseHttpStatus: 200,
          responsePagePath: "/index.html",
          ttl: cdk.Duration.seconds(0),
        },
      ],
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
    };

    // Add custom domain + certificate if provided
    if (props.certificateArn && props.domainNames?.length) {
      Object.assign(distributionProps, {
        domainNames: props.domainNames,
        certificate: { certificateArn: props.certificateArn },
      });
    }

    this.distribution = new cloudfront.Distribution(this, "Distribution", distributionProps);

    // Deploy frontend build output to S3 (skipped if frontend hasn't been built yet).
    // Build frontend first: cd frontend && NEXT_OUTPUT=export pnpm build
    const frontendOutDir = path.join(__dirname, "..", "..", "frontend", "out");
    if (fs.existsSync(frontendOutDir)) {
      new s3deploy.BucketDeployment(this, "DeployFrontend", {
        sources: [s3deploy.Source.asset(frontendOutDir)],
        destinationBucket: this.siteBucket,
        distribution: this.distribution,
        distributionPaths: ["/*"],
      });
    }

    // cdk-nag suppressions
    NagSuppressions.addResourceSuppressions(
      this.distribution,
      [
        {
          id: "AwsSolutions-CFR1",
          reason: "Geo restriction not required — internal tool accessed by distributed team.",
        },
        {
          id: "AwsSolutions-CFR2",
          reason: "WAF on CloudFront not required — frontend is static content with no server-side logic.",
        },
        {
          id: "AwsSolutions-CFR4",
          reason: "TLS 1.2 2021 security policy is configured via minimumProtocolVersion.",
        },
      ],
      true,
    );

    NagSuppressions.addResourceSuppressions(
      this.siteBucket,
      [
        {
          id: "AwsSolutions-S1",
          reason:
            "Access logging not required for static frontend bucket — " +
            "CloudFront access logs provide request-level visibility.",
        },
      ],
      true,
    );

    new cdk.CfnOutput(this, "FrontendUrl", {
      value: `https://${this.distribution.distributionDomainName}`,
      description: "CloudFront distribution URL for the frontend",
    });

    new cdk.CfnOutput(this, "DistributionId", {
      value: this.distribution.distributionId,
      description: "CloudFront distribution ID (for cache invalidation)",
    });
  }
}
