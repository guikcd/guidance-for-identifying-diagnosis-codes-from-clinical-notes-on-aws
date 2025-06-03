from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_s3 as s3,
    aws_iam as iam,
    aws_cognito as cognito,
    aws_lambda as lambda_,
    aws_apigateway as apigateway,
    aws_logs as logs,
    aws_s3_deployment as s3deploy,
    aws_events as events,
    aws_events_targets as targets,
    aws_ec2 as ec2,
    aws_sagemaker as sagemaker,
    triggers
)
from constructs import Construct
from cdklabs.generative_ai_cdk_constructs import bedrock
from cdk_nag import NagSuppressions

class PatientDiagnosisSummaryStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.template_options.description = "Guidance for identifying diagnosis codes from clinical notes on AWS (SO9558)"
        # Get context values from the CDK context
        bucket_name = self.node.try_get_context("BucketName")
        self.cdk_default_region = self.node.try_get_context("DefaultRegion")
        self.default_account = self.node.try_get_context("AccountId")
        self.needs_api_setup = self.node.try_get_context("NeedsAPI")
        self.needs_sagemaker_domain = self.node.try_get_context("NeedsSageMakerDomain")

        # suppress CDK nag related errors
        self.suppress_cdknag()

        # Create S3 access log bucket for storing access logs
        access_log_bucket_input = self.setup_s3_access_log_bucket(bucket_name+"datainput")

        # Create bucket for data ingestion
        self.ingestion_bucket = self.setupS3Bucket(bucket_name, access_log_bucket_input)

        # Upload sample patients note for testing
        s3deploy.BucketDeployment(
            self, id='samplepatientsnotes',
            sources=[s3deploy.Source.asset('src/sample_patients_notes')],
            destination_bucket=self.ingestion_bucket,
            destination_key_prefix='gen-ai/icd-10/input/'
        )

        # Setup Bucket read/write policy
        self.bucket_read_write_policy = iam.PolicyStatement(
            actions=["s3:ListBucket", "s3:GetObject", "s3:PutObject"],
            resources=[self.ingestion_bucket.bucket_arn, f"{self.ingestion_bucket.bucket_arn}/*"],
        )

        # CloudWatch log permission
        self.cloudwatch_log_permission = iam.PolicyStatement(
            actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            effect=iam.Effect.ALLOW,
            resources=["*"],
        )

         # Create S3 access log bucket for storing access logs
        access_log_bucket_kb = self.setup_s3_access_log_bucket(bucket_name+"knowledgebase")

        # Setup knowledge base for Bedrock
        self.setup_knowledgebase(access_log_bucket_kb)

         # Bedrock Access policy
        bedrock_resource_name_claude_3 = f"arn:aws:bedrock:{self.cdk_default_region}::foundation-model/anthropic.claude-3-haiku-20240307-v1:0"
        bedrock_resource_name_claude_3_sonnet = f"arn:aws:bedrock:{self.cdk_default_region}::foundation-model/anthropic.claude-3-sonnet-20240229-v1:0"
        bedrock_knowledgebase_resource_name = f"arn:aws:bedrock:{self.cdk_default_region}:{self.default_account}:knowledge-base/{self.GENERAL_KB.knowledge_base_id}"        
        self.bedrock_access_policy = iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Retrieve"],
            effect=iam.Effect.ALLOW,
            resources=[bedrock_resource_name_claude_3, bedrock_resource_name_claude_3_sonnet, bedrock_knowledgebase_resource_name],
        )

        self.comprehend_access_policy = iam.PolicyStatement(
            actions=["comprehendmedical:InferICD10CM"],
            effect=iam.Effect.ALLOW,
            resources=["*"],
        )


        if self.needs_api_setup:
            # Setup API Gateway for REST API along with Lambda function
            self.setup_api()
        else:        
            self.process_text_lambda()

        # Create SageMaker Domain if needed
        if self.needs_sagemaker_domain:
            self.create_sagemaker_domain()

    def setup_s3_access_log_bucket(self, bucket_name):
        """
        Create an S3 bucket for storing access logs.

        Args:
            bucket_name (str): The name of the bucket.

        Returns:
            s3.Bucket: The S3 bucket for access logs.
        """
        return s3.Bucket(
            self,
            "AccessLogBucket"+ bucket_name,
            bucket_name=f"access-log-s3bucket-{bucket_name}",
            object_ownership=s3.ObjectOwnership.OBJECT_WRITER,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True
        )

    def setupS3Bucket(self, bucket_name: str, access_log_bucket: s3.Bucket):
        """
        Create an S3 bucket for data ingestion.

        Args:
            bucket_name (str): The name of the bucket.
            access_log_bucket (s3.Bucket): The S3 bucket for access logs.

        Returns:
            s3.Bucket: The S3 bucket for data ingestion.
        """
        # Create the CORS rule
        cors_rule = s3.CorsRule(
            allowed_methods=[s3.HttpMethods.GET],
            allowed_origins=["allowedOrigins"],
            allowed_headers=["allowedHeaders"],
        )

        # Create the main bucket
        bucket = s3.Bucket(
            self,
            bucket_name,
            bucket_name=bucket_name,
            encryption=s3.BucketEncryption.S3_MANAGED,
            versioned=False,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            enforce_ssl=True,
            cors=[cors_rule],
            event_bridge_enabled=True,
            server_access_logs_bucket=access_log_bucket
        )

        # Output the bucket name
        CfnOutput(self, "BucketName", value=bucket.bucket_name)
        return bucket

    def setup_app_pool(self):
        """
        Set up the Cognito User Pool and App Client for authentication.
        """
        self.authorizer_user_pool = cognito.UserPool(
            self,
            "UserPool",
            self_sign_up_enabled=True,
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True
            )
        )

        user_pool_client = self.authorizer_user_pool.add_client(
            "website-client",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(
                user_password=True,
                user_srp=True
            )
        )

        self.authorizer_user_pool.add_domain(
            "cognito-domain",  # domain name
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix="patientdiagnosissummary"  # prefix for the domain name
            )
        )

    def setup_authorizer(self):
        """
        Set up the API Gateway Authorizer using the Cognito User Pool.
        """
        self.api_authorizer = apigateway.CognitoUserPoolsAuthorizer(
            self, "api-authorizer",
            cognito_user_pools=[self.authorizer_user_pool],
            #identity_source="method.request.header.Authorization",
        )

    def setup_api(self):
        """
        Set up the API Gateway and related resources.
        """
        self.setup_app_pool()

        rest_api_policy_statements = [
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[iam.AccountPrincipal(self.default_account)],
                actions=["execute-api:Invoke"],
                resources=["execute-api:/*"],
            ),
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[iam.AnyPrincipal()],
                actions=["execute-api:Invoke"],
                resources=["execute-api:/*/OPTIONS/*"],
            ),
        ]

        log_group = logs.LogGroup(self, "ApiGatewayAccessLogs")

        self.rest_api = apigateway.RestApi(
            self, "PatientDiagnosisSummary-api",
            rest_api_name="Patients Diagnosis Summary API",
            description="This API is use to get diagnosis summary of patient's visit.", 
            default_cors_preflight_options=apigateway.CorsOptions(
                allow_origins=apigateway.Cors.ALL_ORIGINS,
                allow_methods=apigateway.Cors.ALL_METHODS,
                allow_headers=apigateway.Cors.DEFAULT_HEADERS,
            ),
            policy=iam.PolicyDocument(
                statements=rest_api_policy_statements,
            ),
            cloud_watch_role=True,          
            deploy_options=apigateway.StageOptions(
                stage_name="dev",
                logging_level=apigateway.MethodLoggingLevel.ERROR,
                data_trace_enabled=True,  # Enable data tracing for debugging
                access_log_destination=apigateway.LogGroupLogDestination(log_group),
                access_log_format=apigateway.AccessLogFormat.json_with_standard_fields(
                            caller=False,
                            http_method=True,
                            ip=True,
                            protocol=True,
                            request_time=True,
                            resource_path=True,
                            response_length=True,
                            status=True,
                            user=True
                        )
            ),
        )

        # Setup API Authorizer
        self.setup_authorizer()

        # Setup API
        self.process_text_api()

    def process_text_lambda(self):
        """
        Create the Lambda function for processing text.
        """
        lambda_environment_variables = {
            "KnowledgeBaseId": self.GENERAL_KB.knowledge_base_id,
            "NumberOfResults": "5",
            "DefaultRegion": self.cdk_default_region
        }

        processtext_lambda = lambda_.Function(
            self,
            "processing-lambda",
            code=lambda_.Code.from_asset("src/Lambda/processicd10code"),
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="processicd10code.lambda_handler",
            timeout=Duration.seconds(600),
            environment=lambda_environment_variables,
            #role=lambda_role
        )

        processtext_lambda.add_to_role_policy(self.bedrock_access_policy)
        processtext_lambda.add_to_role_policy(self.cloudwatch_log_permission)
        processtext_lambda.add_to_role_policy(self.bucket_read_write_policy)
        processtext_lambda.add_to_role_policy(self.comprehend_access_policy) 

        # Output the Lambda Function
        CfnOutput(
            self,
            "LambdaProcessICD10Code",
            value=processtext_lambda.function_name,
            description="Lambda Process ICD10 Code",
        )      

        return processtext_lambda

    def process_text_api(self):
        """
        Set up the API Gateway resource and method for processing text.
        """
        processtext_lambda = self.process_text_lambda()
        processtext_integration = apigateway.LambdaIntegration(processtext_lambda)

        self.rest_api.root.add_resource("processtext").add_method(
            "POST",
            processtext_integration,            
            authorization_type=apigateway.AuthorizationType.COGNITO,
            authorizer=self.api_authorizer,
        )

    def knowledgebase_ingestion_process_lambda(self, dataSourceId, knowledgeBaseId):
        """
        Create the Lambda function for knowledge base ingestion.

        Args:
            dataSourceId (str): The ID of the data source.
            knowledgeBaseId (str): The ID of the knowledge base.

        Returns:
            lambda_.Function: The Lambda function for knowledge base ingestion.
        """
        lambda_environment_variables = {
            "DataSourceId": self.ingestion_bucket.bucket_name,
            "KnowledgeBaseId": "api_results"
        }

        ingestionJob_lambda = lambda_.Function(
            self,
            "ingestionJob-lambda",
            code=lambda_.Code.from_asset("src/Lambda/knowledgebase-IngestionJob"),
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="ingestionJob.lambda_handler",
            timeout=Duration.seconds(600),
            environment={
                "DataSourceId": dataSourceId,
                "KnowledgeBaseId": knowledgeBaseId
            },
        )
        ingestionJob_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:StartIngestionJob",
                    "bedrock:GetIngestionJob"
                ],
                resources=[
                    "*"
                ]
            )
        )

        ingestionJob_lambda.add_to_role_policy(self.cloudwatch_log_permission)
        return ingestionJob_lambda

   
    
    def setup_knowledgebase(self, access_log_bucket: s3.Bucket):
        """
        Set up the knowledge base for Bedrock and configure the necessary resources.
        """
        # Create a knowledge base for general questions related to the company's services and offerings
        self.GENERAL_KB = bedrock.KnowledgeBase(
            self, id='PatientDiagnosisSummaryKnowledgeBase',
            embeddings_model=bedrock.BedrockFoundationModel.TITAN_EMBED_TEXT_V1,
            instruction='Use this knowledge base for answering general questions related to the company name services and offerings ' +
                        'It contains text of FAQ documents like how to change password, how to cancel subscription.'
        )

        # Create an S3 bucket to store the knowledge base artifacts
        patientdiagnosissummary_extraction_knowledgebase = s3.Bucket(
            self, id='knowledgebase',
            enforce_ssl=True,
            versioned=False,
            public_read_access=False,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            server_access_logs_bucket=access_log_bucket,
            server_access_logs_prefix='BucketLogs/',
            event_bridge_enabled=True
        )

        # Create a data source for the knowledge base from the S3 bucket
        kbDatasource = bedrock.S3DataSource(
            self, id='PatientDiagnosisSummaryKnowledgeBaseDataSource',
            bucket=patientdiagnosissummary_extraction_knowledgebase,
            knowledge_base=self.GENERAL_KB,
            data_source_name='PatientDiagnosisSummaryKnowledgeBaseDataSource',
            chunking_strategy=bedrock.ChunkingStrategy.DEFAULT,
            max_tokens=500,
            overlap_percentage=20
        )

        # Output the Bedrock Knowledge Base ID
        CfnOutput(
            self,
            "BedrockKnowledgeBaseID",
            value=self.GENERAL_KB.knowledge_base_id,
            description="Bedrock Knowledge Base ID",
        )

        # Set up a Lambda function to handle the knowledge base ingestion job
        ingestionJob_lambda = self.knowledgebase_ingestion_process_lambda(kbDatasource.data_source_id, self.GENERAL_KB.knowledge_base_id)

        # Set up an S3 event trigger for the knowledge base bucket
        upload_event = events.Rule(
            self,
            "S3UploadEvent",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {"name": [patientdiagnosissummary_extraction_knowledgebase.bucket_name]},
                    "object": {"size": [{"numeric": [">", 0]}]}
                }
            )
        )
        upload_event.add_target(targets.LambdaFunction(ingestionJob_lambda))

        # Upload necessary documents to the S3 knowledge base bucket
        s3deploy.BucketDeployment(
            self, id='UpdateKnowledgeBaseArtifacts',
            sources=[s3deploy.Source.asset('src/knowledgebase_docs')],
            destination_bucket=patientdiagnosissummary_extraction_knowledgebase
        )

        # Trigger Ingenstion lambda function after the resource creation.
        triggers.Trigger(
            self,
            "IngestionJobTrigger",
            handler=ingestionJob_lambda,
            invocation_type=triggers.InvocationType.EVENT,
        )

    def create_sagemaker_domain(self):
        """
        Create a SageMaker Domain with the necessary resources.
        """
        # Create a VPC with two private subnets
        vpc = ec2.Vpc(self, "SageMakerVPC", max_azs=2, nat_gateways=1)
        selected_subnets = vpc.select_subnets(one_per_az=True, subnet_type=ec2.SubnetType.PRIVATE_WITH_NAT)

        # Create a log group for VPC Flow Logs
        log_group = logs.LogGroup(self, "VPCFlowLogsGroup",
            retention=logs.RetentionDays.ONE_WEEK
        )

        # Create an IAM role for VPC Flow Logs
        flow_log_role = iam.Role(self, "VPCFlowLogsRole",
            assumed_by=iam.ServicePrincipal("vpc-flow-logs.amazonaws.com")
        )

        # Add necessary permissions to the role
        flow_log_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
                "logs:DescribeLogGroups",
                "logs:DescribeLogStreams"
            ],
            resources=["arn:aws:logs:*:*:*"]
        ))

        # Add Flow Logs to the VPC
        ec2.FlowLog(self, "VPCFlowLogs",
            resource_type=ec2.FlowLogResourceType.from_vpc(vpc),
            destination=ec2.FlowLogDestination.to_cloud_watch_logs(log_group, flow_log_role)
        )

        # Create an IAM Role for SageMaker Domain
        sagemaker_role = iam.Role(
            self,
            "SageMakerDomainRole",
            assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSageMakerFullAccess"),                
            ],
            inline_policies={
                "sagemaker-policy": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            effect=iam.Effect.ALLOW,
                            actions=[
                                "logs:CreateLogGroup",
                                "logs:CreateLogStream",
                                "logs:PutLogEvents",
                                "comprehendmedical:InferICD10CM",
                                "bedrock:InvokeModel",
                                "bedrock:Retrieve",
                                "s3:GetObject",
				                "s3:ListBucket",
				                "s3:PutObject"
                            ],
                            resources=["*"],
                        ),
                    ],
                ),
            }
        )

        # Create the SageMaker Domain
        sagemaker_domain = sagemaker.CfnDomain(
            self,
            "SageMakerDomain",
            domain_name="patient-history",
            auth_mode="IAM",
            subnet_ids=selected_subnets.subnet_ids,
            vpc_id=vpc.vpc_id,
            default_user_settings=sagemaker.CfnDomain.UserSettingsProperty(
                execution_role=sagemaker_role.role_arn,
                studio_web_portal="ENABLED",
                default_landing_uri="studio::"
            )
        )

        # Create a SageMaker User Profile
        user_profile = sagemaker.CfnUserProfile(
            self,
            "SageMakerUserProfile",
            domain_id=sagemaker_domain.attr_domain_id,
            user_profile_name="my-user-profile",
            user_settings={
                "executionRole": sagemaker_role.role_arn,
                "jupyterServerAppSettings": {
                    "defaultResourceSpec": {
                        "instanceType": "system",
                        "lifecycleConfigArn": "arn:aws:sagemaker:" + self.cdk_default_region + ":0274:life-cycle-config/default-config"
                    }
                }
            },
        )

        # Output the SageMaker Domain ID and User Profile ARN
        CfnOutput(
            self,
            "SageMakerDomainId",
            value=sagemaker_domain.attr_domain_id,
            description="SageMaker Domain ID",
        )
        CfnOutput(
            self,
            "SageMakerUserProfileArn",
            value=user_profile.attr_user_profile_arn,
            description="SageMaker User Profile ARN",
        )

    def suppress_cdknag(self):
        #NagSuppressions.add_stack_suppressions(self, [{"id":"AwsSolutions-APIG4", "reason":"Suppress AwsSolutions-APIG4 as API Gateway has cognito authetication"}])
        #NagSuppressions.add_stack_suppressions(self, [{"id":"AwsSolutions-COG4", "reason":"Suppress AwsSolutions-COG4 as API Gateway uses cognito user pool for authetication"}])
        NagSuppressions.add_stack_suppressions(self, [{"id":"AwsSolutions-IAM4", "reason":"Suppress AwsSolutions-IAM4 for CDK built-in constructs"}])
        NagSuppressions.add_stack_suppressions(self, [{"id":"AwsSolutions-IAM5", "reason":"Suppress AwsSolutions-IAM5 for CDK built-in constructs"}])
        NagSuppressions.add_stack_suppressions(self, [{"id":"AwsSolutions-L1", "reason":"Lambda function is using the python 3.11 version based on the its dependency."}])
        #NagSuppressions.add_stack_suppressions(self, [{"id":"AwsSolutions-APIG2", "reason":"Suppress AwsSolutions-APIG2 as customer will decide on request."}])
        #NagSuppressions.add_stack_suppressions(self, [{"id":"AwsSolutions-COG1", "reason":"Suppress AwsSolutions-COG1 as it has already password policy for 8 characters."}])
        #NagSuppressions.add_stack_suppressions(self, [{"id":"AwsSolutions-COG2", "reason":"Suppress AwsSolutions-COG2 as MFA will be taken care for customer."}])
        #NagSuppressions.add_stack_suppressions(self, [{"id":"AwsSolutions-COG3", "reason":"Suppress AwsSolutions-COG3 as this prototype does not need for ENFOURCE AdvanceSecurityMode."}])
