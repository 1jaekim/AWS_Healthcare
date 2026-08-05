"""프론트엔드 정적 호스팅 — S3 + CloudFront.

`frontend/scripts/deploy-s3.sh` 가 이미 이 구성을 전제로 쓰여 있다. 그 스크립트의
주석이 요구하는 두 가지 — OAC 로만 읽는 비공개 버킷, 403/404 를 index.html 로
되돌리는 오류 응답 — 를 여기서 코드로 못박는다. 콘솔에서 손으로 맞추면 다음 사람이
같은 함정을 다시 밟는다.

오류 응답이 왜 필요한가: React Router 는 `/results/medi25-10842` 같은 주소를
브라우저 안에서 처리한다. 그 주소로 새로고침하면 S3 에는 그런 키가 없어서 403
(비공개 버킷이므로 404 가 아니라 403 이다)이 오고 화면이 깨진다. index.html 을
200 으로 돌려줘야 앱이 뜨고 라우터가 경로를 해석한다.
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_s3 as s3,
)
from constructs import Construct


class FrontendStack(Stack):
    """SPA 정적 호스팅."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # 빌드 산출물만 들어가는 버킷이다. 언제든 다시 만들 수 있으므로 스택을
        # 지울 때 같이 지운다 — 데이터 레이크와 달리 여기엔 잃을 것이 없다.
        self.bucket = s3.Bucket(
            self,
            "FrontendBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        spa_fallback = [
            cloudfront.ErrorResponse(
                http_status=status,
                response_http_status=200,
                response_page_path="/index.html",
                ttl=Duration.seconds(0),
            )
            # 403 이 먼저다. OAC 로 잠근 버킷은 없는 키에 404 가 아니라 403 을
            # 준다(존재 여부를 흘리지 않기 위해서다). 404 만 처리하면 딥링크가
            # 그대로 깨진다.
            for status in (403, 404)
        ]

        self.distribution = cloudfront.Distribution(
            self,
            "FrontendDistribution",
            comment="임상시험 매칭 프론트엔드",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                compress=True,
            ),
            error_responses=spa_fallback,
            # 한국 사용자 대상이라 가장 싼 등급으로 충분하다. PriceClass 100 은
            # 북미·유럽만 쓰므로 아시아 요청이 멀리 돈다.
            price_class=cloudfront.PriceClass.PRICE_CLASS_200,
        )

        CfnOutput(
            self,
            "FrontendBucketName",
            value=self.bucket.bucket_name,
            description="deploy-s3.sh 의 S3_BUCKET",
        )
        CfnOutput(
            self,
            "DistributionId",
            value=self.distribution.distribution_id,
            description="deploy-s3.sh 의 CLOUDFRONT_DISTRIBUTION_ID",
        )
        CfnOutput(
            self,
            "FrontendUrl",
            value=f"https://{self.distribution.distribution_domain_name}",
            description="접속 주소. API 의 CORS_ALLOW_ORIGINS 에도 이 값을 넣는다",
        )
