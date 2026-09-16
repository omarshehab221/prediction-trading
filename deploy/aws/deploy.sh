#!/usr/bin/env bash
# Deploy the bot to ECS Fargate in Tokyo (ap-northeast-1).
#
# Idempotent: every run creates whatever infrastructure is missing, builds the
# committed HEAD in CodeBuild (skipped if that commit's image is already in
# ECR), registers a task definition and rolls the service onto it.
#
#   ./deploy/aws/deploy.sh
#
# Needs: aws CLI, git, and a secret named btc5m/binance holding
#   {"BINANCE_API_KEY": "...", "BINANCE_API_SECRET": "..."}
#
# THINGS THAT WILL BREAK THIS IF IGNORED
# --------------------------------------
# 1. NEVER TWO TASKS. Two live processes trade the same account twice. The
#    service runs one task and deploys with maximumPercent=100, so the old
#    task stops before the new one starts. Do not raise either, and do not run
#    the Render worker alongside this.
#
# 2. /var/data IS EFS. Config and journal live there and survive redeploys.
#    Reset the config by deleting it from inside the task:
#      aws ecs execute-command --cluster precdiction-trading --interactive \
#        --task <id> --container btc-5m-predictor --command "rm /var/data/config.json"
#
# 3. THE OUTBOUND IP CHANGES ON EVERY RESTART. Tasks get a public IP, not a
#    fixed one. Preflight prints the address and waits AUTH_WAIT_S for it to be
#    allowlisted on the Binance key, exactly as it did on Render.
#
# 4. SHUTDOWN IS CAPPED AT 120 SECONDS. Fargate's stopTimeout maximum. A
#    redeploy mid-round gives the bot two minutes, not the rest of the round.
#
# 5. ONLY COMMITTED CODE SHIPS. The build uploads `git archive HEAD`, so
#    uncommitted edits are silently absent from the image.

set -euo pipefail

# Git Bash on Windows rewrites arguments that look like paths (/ecs/...).
export MSYS_NO_PATHCONV=1
export AWS_PAGER=""

REGION="${AWS_REGION:-ap-northeast-1}"
CLUSTER="${CLUSTER:-precdiction-trading}"
APP="btc-5m-predictor"
SECRET_NAME="${SECRET_NAME:-btc5m/binance}"
# live by default. TRADING_MODE=shadow runs the live code path on live data
# with no order, cancel or redeem sent, and journals to its own file so a
# shadow session never mixes with the live calibration record.
TRADING_MODE="${TRADING_MODE:-live}"
case "$TRADING_MODE" in
  live)   DEFAULT_DB="btc5m_journal.db" ;;
  shadow) DEFAULT_DB="shadow_journal.db" ;;
  *) echo "FATAL: TRADING_MODE must be live or shadow, got '$TRADING_MODE'" >&2
     exit 1 ;;
esac
DB_PATH="${DB_PATH:-/var/data/$DEFAULT_DB}"
# The strategy the bot runs. Only used when the config file is first written
# -- entrypoint.sh keeps an existing one -- so switching profiles means
# regenerating /var/data/config.json as well as setting this.
PROFILE="${PROFILE:-scalp}"
# The config is kept once written, so a different profile needs its own file
# (e.g. CONFIG_PATH=/var/data/config-lock.json) rather than a deleted one.
CONFIG_PATH="${CONFIG_PATH:-/var/data/config.json}"
case "$PROFILE" in
  scalp|straddle|lock|lastminute|balanced|buffer|convex|favorite|micro) ;;
  *) echo "FATAL: unknown PROFILE '$PROFILE'" >&2; exit 1 ;;
esac

export AWS_DEFAULT_REGION="$REGION"

cd "$(git rev-parse --show-toplevel)"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REPO_URI="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$APP"
BUCKET="$APP-build-$ACCOUNT-$REGION"
LOG_GROUP="/ecs/$APP"
EXEC_ROLE="$APP-execution"
TASK_ROLE="$APP-task"
BUILD_ROLE="$APP-codebuild"
TAG="$(git rev-parse --short=12 HEAD)"

log() { printf '\n== %s\n' "$*"; }

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "WARNING: uncommitted changes are NOT deployed; building $TAG." >&2
fi

# --- IAM ---------------------------------------------------------------------

ensure_role() {  # name service
  local name="$1" service="$2"
  if ! aws iam get-role --role-name "$name" >/dev/null 2>&1; then
    aws iam create-role --role-name "$name" --assume-role-policy-document \
      "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"$service\"},\"Action\":\"sts:AssumeRole\"}]}" \
      >/dev/null
    echo "created role $name"
  fi
}

log "IAM roles"
SECRET_ARN="$(aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --query ARN --output text)"

ensure_role "$EXEC_ROLE" ecs-tasks.amazonaws.com
aws iam attach-role-policy --role-name "$EXEC_ROLE" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam put-role-policy --role-name "$EXEC_ROLE" --policy-name read-binance-secret \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"secretsmanager:GetSecretValue\",\"Resource\":\"$SECRET_ARN\"}]}"

# The task role exists for ECS Exec, which is how you reach the config on EFS.
ensure_role "$TASK_ROLE" ecs-tasks.amazonaws.com
aws iam put-role-policy --role-name "$TASK_ROLE" --policy-name ecs-exec \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["ssmmessages:CreateControlChannel","ssmmessages:CreateDataChannel","ssmmessages:OpenControlChannel","ssmmessages:OpenDataChannel"],"Resource":"*"}]}'

ensure_role "$BUILD_ROLE" codebuild.amazonaws.com
aws iam put-role-policy --role-name "$BUILD_ROLE" --policy-name build-and-push \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[
    {\"Effect\":\"Allow\",\"Action\":[\"logs:CreateLogGroup\",\"logs:CreateLogStream\",\"logs:PutLogEvents\"],\"Resource\":\"arn:aws:logs:$REGION:$ACCOUNT:log-group:/aws/codebuild/$APP*\"},
    {\"Effect\":\"Allow\",\"Action\":\"s3:GetObject\",\"Resource\":\"arn:aws:s3:::$BUCKET/*\"},
    {\"Effect\":\"Allow\",\"Action\":\"ecr:GetAuthorizationToken\",\"Resource\":\"*\"},
    {\"Effect\":\"Allow\",\"Action\":[\"ecr:BatchCheckLayerAvailability\",\"ecr:InitiateLayerUpload\",\"ecr:UploadLayerPart\",\"ecr:CompleteLayerUpload\",\"ecr:PutImage\",\"ecr:BatchGetImage\"],\"Resource\":\"arn:aws:ecr:$REGION:$ACCOUNT:repository/$APP\"}]}"

# --- Registry, build bucket, logs --------------------------------------------

log "ECR, S3, CloudWatch Logs"
if ! aws ecr describe-repositories --repository-names "$APP" >/dev/null 2>&1; then
  # Immutable tags: a commit's tag always means that commit's image.
  aws ecr create-repository --repository-name "$APP" \
    --image-tag-mutability IMMUTABLE \
    --image-scanning-configuration scanOnPush=true >/dev/null
  aws ecr put-lifecycle-policy --repository-name "$APP" --lifecycle-policy-text \
    '{"rules":[{"rulePriority":1,"description":"keep last 20","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":20},"action":{"type":"expire"}}]}' \
    >/dev/null
  echo "created repository $APP"
fi

if ! aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  aws s3api create-bucket --bucket "$BUCKET" \
    --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
  aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration \
    '{"Rules":[{"ID":"expire-sources","Status":"Enabled","Filter":{"Prefix":"source/"},"Expiration":{"Days":30}}]}'
  echo "created bucket $BUCKET"
fi

if [ "$(aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" \
        --query "logGroups[?logGroupName=='$LOG_GROUP'] | length(@)" --output text)" = "0" ]; then
  aws logs create-log-group --log-group-name "$LOG_GROUP"
  echo "created log group $LOG_GROUP"
fi
# Retention is housekeeping, not a reason to abandon a deploy halfway through.
if [ "$(aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" \
        --query "logGroups[?logGroupName=='$LOG_GROUP'].retentionInDays | [0]" --output text)" = "None" ]; then
  aws logs put-retention-policy --log-group-name "$LOG_GROUP" --retention-in-days 30 \
    || echo "WARNING: could not set log retention; logs are kept forever." >&2
fi

# --- Network -----------------------------------------------------------------

log "Network"
VPC="$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)"
SUBNETS="$(aws ec2 describe-subnets --filters Name=vpc-id,Values="$VPC" Name=default-for-az,Values=true \
  --query 'Subnets[].SubnetId' --output text)"

ensure_sg() {  # name description -> prints id
  local id
  id="$(aws ec2 describe-security-groups --filters Name=vpc-id,Values="$VPC" Name=group-name,Values="$1" \
    --query 'SecurityGroups[0].GroupId' --output text)"
  if [ "$id" = "None" ]; then
    id="$(aws ec2 create-security-group --vpc-id "$VPC" --group-name "$1" --description "$2" \
      --query GroupId --output text)"
  fi
  echo "$id"
}

# No ingress: the bot only dials out.
TASK_SG="$(ensure_sg "$APP-task" "btc-5m-predictor task, egress only")"
EFS_SG="$(ensure_sg "$APP-efs" "btc-5m-predictor EFS, NFS from the task")"
aws ec2 authorize-security-group-ingress --group-id "$EFS_SG" --protocol tcp --port 2049 \
  --source-group "$TASK_SG" >/dev/null 2>&1 || true
echo "vpc=$VPC task_sg=$TASK_SG efs_sg=$EFS_SG"

# --- EFS (/var/data) ---------------------------------------------------------

log "EFS"
FS_ID="$(aws efs describe-file-systems --creation-token "$APP" --query 'FileSystems[0].FileSystemId' --output text)"
if [ "$FS_ID" = "None" ]; then
  # No --tags: the creation token is the lookup key, and tagging needs
  # elasticfilesystem:TagResource, which the deploying user may not have.
  FS_ID="$(aws efs create-file-system --creation-token "$APP" --encrypted \
    --performance-mode generalPurpose --throughput-mode elastic \
    --query FileSystemId --output text)"
  echo "created file system $FS_ID"
fi
until [ "$(aws efs describe-file-systems --file-system-id "$FS_ID" --query 'FileSystems[0].LifeCycleState' --output text)" = "available" ]; do
  sleep 5
done

# Text output separates list items with tabs; the match below needs spaces.
EXISTING_MT_SUBNETS="$(aws efs describe-mount-targets --file-system-id "$FS_ID" --query 'MountTargets[].SubnetId' --output text | tr '\t' ' ')"
for subnet in $SUBNETS; do
  case " $EXISTING_MT_SUBNETS " in
    *" $subnet "*) ;;
    *) aws efs create-mount-target --file-system-id "$FS_ID" --subnet-id "$subnet" \
         --security-groups "$EFS_SG" >/dev/null
       echo "created mount target in $subnet" ;;
  esac
done
until [ "$(aws efs describe-mount-targets --file-system-id "$FS_ID" \
          --query "MountTargets[?LifeCycleState!='available'] | length(@)" --output text)" = "0" ]; do
  sleep 5
done
echo "file system $FS_ID ready"

# --- Build -------------------------------------------------------------------

log "Image $REPO_URI:$TAG"
if aws ecr describe-images --repository-name "$APP" --image-ids imageTag="$TAG" >/dev/null 2>&1; then
  echo "already in ECR, skipping build"
else
  if [ "$(aws codebuild batch-get-projects --names "$APP" --query 'length(projects)' --output text)" = "0" ]; then
    # A freshly created role takes a few seconds to become assumable.
    for attempt in 1 2 3 4 5 6; do
      if aws codebuild create-project --name "$APP" \
           --source "type=S3,location=$BUCKET/source/placeholder.zip,buildspec=deploy/aws/buildspec.yml" \
           --artifacts type=NO_ARTIFACTS \
           --environment "type=LINUX_CONTAINER,image=aws/codebuild/amazonlinux-x86_64-standard:5.0,computeType=BUILD_GENERAL1_SMALL,privilegedMode=true,environmentVariables=[{name=REPO_URI,value=$REPO_URI}]" \
           --service-role "arn:aws:iam::$ACCOUNT:role/$BUILD_ROLE" >/dev/null; then
        echo "created CodeBuild project $APP"; break
      fi
      [ "$attempt" = 6 ] && exit 1
      sleep 10
    done
  fi

  archive="$(mktemp -d)/source.zip"
  # git and aws are native Windows programs under Git Bash; /tmp means nothing
  # to either, so hand them the Windows form of the path.
  case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) archive="$(cygpath -w "$archive")" ;; esac
  git archive --format=zip -o "$archive" HEAD
  aws s3 cp "$archive" "s3://$BUCKET/source/$TAG.zip" --only-show-errors

  BUILD_ID="$(aws codebuild start-build --project-name "$APP" \
    --source-location-override "$BUCKET/source/$TAG.zip" \
    --environment-variables-override "name=IMAGE_TAG,value=$TAG" \
    --query build.id --output text)"
  echo "build $BUILD_ID"
  while :; do
    status="$(aws codebuild batch-get-builds --ids "$BUILD_ID" --query 'builds[0].buildStatus' --output text)"
    [ "$status" != "IN_PROGRESS" ] && break
    sleep 15
  done
  if [ "$status" != "SUCCEEDED" ]; then
    echo "FATAL: build $status. Logs:" >&2
    aws codebuild batch-get-builds --ids "$BUILD_ID" --query 'builds[0].logs.deepLink' --output text >&2
    exit 1
  fi
  echo "build SUCCEEDED"
fi

# --- Task definition ---------------------------------------------------------

log "Task definition"
# Mirrors render.yaml's envVars. SYMBOLS is pinned: with it empty the scalp
# strategy's futures feed subscribed to nothing and the bot never traded.
TASK_DEF="$(cat <<JSON
{
  "family": "$APP",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "512",
  "memory": "1024",
  "runtimePlatform": {"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
  "executionRoleArn": "arn:aws:iam::$ACCOUNT:role/$EXEC_ROLE",
  "taskRoleArn": "arn:aws:iam::$ACCOUNT:role/$TASK_ROLE",
  "volumes": [{
    "name": "data",
    "efsVolumeConfiguration": {"fileSystemId": "$FS_ID", "transitEncryption": "ENABLED"}
  }],
  "containerDefinitions": [{
    "name": "$APP",
    "image": "$REPO_URI:$TAG",
    "essential": true,
    "stopTimeout": 120,
    "linuxParameters": {"initProcessEnabled": true},
    "mountPoints": [{"sourceVolume": "data", "containerPath": "/var/data"}],
    "environment": [
      {"name": "CONFIG_PATH", "value": "$CONFIG_PATH"},
      {"name": "DB_PATH", "value": "$DB_PATH"},
      {"name": "PROFILE", "value": "$PROFILE"},
      {"name": "TRADING_MODE", "value": "$TRADING_MODE"},
      {"name": "SYMBOLS", "value": "BTCUSDT,ETHUSDT,BNBUSDT"},
      {"name": "PYTHONUNBUFFERED", "value": "1"},
      {"name": "SKIP_VERIFY", "value": "0"},
      {"name": "PREFLIGHT_REQUIRED", "value": "1"},
      {"name": "AUTH_WAIT_S", "value": "600"}
    ],
    "secrets": [
      {"name": "BINANCE_API_KEY", "valueFrom": "$SECRET_ARN:BINANCE_API_KEY::"},
      {"name": "BINANCE_API_SECRET", "valueFrom": "$SECRET_ARN:BINANCE_API_SECRET::"}
    ],
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {"awslogs-group": "$LOG_GROUP", "awslogs-region": "$REGION", "awslogs-stream-prefix": "bot"}
    }
  }]
}
JSON
)"
TASK_DEF_ARN="$(aws ecs register-task-definition --cli-input-json "$TASK_DEF" \
  --query taskDefinition.taskDefinitionArn --output text)"
echo "$TASK_DEF_ARN"

# --- Service -----------------------------------------------------------------

log "Service $APP on $CLUSTER"
NETWORK="awsvpcConfiguration={subnets=[$(echo $SUBNETS | tr ' ' ',')],securityGroups=[$TASK_SG],assignPublicIp=ENABLED}"
# min 0 / max 100: the old task is gone before the new one trades. See note 1.
DEPLOY_CFG="maximumPercent=100,minimumHealthyPercent=0,deploymentCircuitBreaker={enable=true,rollback=true}"

if [ "$(aws ecs describe-services --cluster "$CLUSTER" --services "$APP" \
        --query 'services[0].status' --output text)" = "ACTIVE" ]; then
  aws ecs update-service --cluster "$CLUSTER" --service "$APP" \
    --task-definition "$TASK_DEF_ARN" --desired-count 1 \
    --network-configuration "$NETWORK" --deployment-configuration "$DEPLOY_CFG" \
    --enable-execute-command >/dev/null
  echo "service updated"
else
  aws ecs create-service --cluster "$CLUSTER" --service-name "$APP" \
    --task-definition "$TASK_DEF_ARN" --desired-count 1 \
    --launch-type FARGATE --platform-version LATEST \
    --network-configuration "$NETWORK" --deployment-configuration "$DEPLOY_CFG" \
    --enable-execute-command --propagate-tags SERVICE >/dev/null
  echo "service created"
fi

cat <<EOF

Deploying $TAG. Follow boot, verify and preflight with:
  MSYS_NO_PATHCONV=1 aws logs tail $LOG_GROUP --follow --region $REGION
EOF
