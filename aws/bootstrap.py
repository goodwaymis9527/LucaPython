#!/usr/bin/env python3
"""bootstrap.py — 只跑一次：把 AWS 端資源建起來（boto3 版，不需要 AWS CLI）

建立：ECR、CloudWatch log group、兩個 IAM role、task SG、ALB + target group + listener、
      ECS cluster、第一版 task definition、ECS service
所有 ARN 寫進 .aws-state/config.json，之後 deploy.py / status.py 直接讀。

執行：
  python aws/bootstrap.py

與 Node 版的差異只有兩處：
  - 容器 port 是 8000（Node 版 3000），target group 的 port 與 health check path 也要對應
  - 資源名稱是 luca-python，不會與已經跑著的 lucaaws 衝突
"""
from __future__ import annotations

import json
import time

from aws_common import (
    PROJECT,
    REGION,
    arn,
    build_task_definition,
    client,
    is_err_type,
    new_state,
    save_state,
    whoami,
)

PORT = 8000
ecr = client("ecr")
logs = client("logs")
iam = client("iam")
ec2 = client("ec2")
elb = client("elbv2")
ecs = client("ecs")

state = new_state()
state["account"] = whoami()

print("=== 1) VPC / subnets ===")
vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
if not vpcs:
    raise SystemExit("此區域沒有 default VPC；請先建 VPC 或手動把 vpcId 寫進 config.json")
vpc = vpcs[0]

subnets = [s["SubnetId"] for s in ec2.describe_subnets(
    Filters=[{"Name": "vpc-id", "Values": [vpc["VpcId"]]}, {"Name": "state", "Values": ["available"]}]
)["Subnets"]]
if not subnets:
    raise SystemExit("VPC 內沒有可用 subnet")

state.update({"region": REGION, "project": PROJECT, "vpcId": vpc["VpcId"], "vpcCidr": vpc["CidrBlock"], "subnets": subnets})
print(f"VPC {vpc['VpcId']}  {vpc['CidrBlock']}  subnet 數={len(subnets)}")

# 沒有 IGW 就沒有 internet 路由：外部連不進 ALB，task 也連不出去 Neon（Neon 在 AWS 之外，一定要走 IGW）
print("=== 2) internet gateway + 0.0.0.0/0 路由 ===")
igws = ec2.describe_internet_gateways().get("InternetGateways", [])
igw = next((g for g in igws if any(a.get("VpcId") == vpc["VpcId"] for a in g.get("Attachments", []))), None)
if not igw:
    igw = igws[0] if igws else ec2.create_internet_gateway()["InternetGateway"]
    try:
        ec2.attach_internet_gateway(InternetGatewayId=igw["InternetGatewayId"], VpcId=vpc["VpcId"])
        print(f"attach IGW: {igw['InternetGatewayId']} → {vpc['VpcId']}")
    except Exception as err:
        if is_err_type(err, "ResourceAlreadyAssociated"):
            print(f"IGW {igw['InternetGatewayId']} 已 attach（duplicate 例外，視為成功）")
        else:
            raise
else:
    print(f"IGW {igw['InternetGatewayId']} 已 attach 到 {vpc['VpcId']}")

tables = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc["VpcId"]]}]).get("RouteTables", [])
table = next((t for t in tables if t.get("Default")), tables[0] if tables else None)
if not table:
    raise SystemExit(f"找不到 {vpc['VpcId']} 的 route table")
igw_id = igw["InternetGatewayId"]
if any(r.get("GatewayId") == igw_id for r in table.get("Routes", [])):
    print(f"route table {table['RouteTableId']} 已有 0.0.0.0/0 → {igw_id}")
else:
    try:
        ec2.create_route(RouteTableId=table["RouteTableId"], DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
        print(f"route table {table['RouteTableId']} 加上 0.0.0.0/0 → {igw_id}")
    except Exception as err:
        if is_err_type(err, "RouteAlreadyExists"):
            print(f"route table {table['RouteTableId']} 已有 0.0.0.0/0（duplicate 例外，視為成功）")
        else:
            raise
state["igwId"] = igw_id
state["routeTableId"] = table["RouteTableId"]

public_subnets = [s["SubnetId"] for s in ec2.describe_subnets(
    Filters=[{"Name": "vpc-id", "Values": [vpc["VpcId"]]}])["Subnets"] if s.get("MapPublicIpOnLaunch")]
if not public_subnets:
    print("警告：VPC 內沒有 mapPublicIpOnLaunch=true 的 subnet，task 拿不到 public IP，連外部服務（Neon）會失敗")

print("=== 3) ECR ===")
try:
    repo = ecr.describe_repositories(repositoryNames=[PROJECT])["repositories"][0]
except Exception as err:
    if not is_err_type(err, "RepositoryNotFoundException"):
        raise
    repo = ecr.create_repository(
        repositoryName=PROJECT,
        imageTagMutability="MUTABLE",
        imageScanningConfiguration={"scanOnPush": True},
    )["repository"]
    print(f"建立 ECR: {repo['repositoryUri']}")
state["ecrUri"] = repo["repositoryUri"]

print("=== 4) CloudWatch log group ===")
log_group = f"/ecs/{PROJECT}"
state["logGroup"] = log_group
try:
    logs.create_log_group(logGroupName=log_group)
    print(f"建立 log group: {log_group}")
except Exception as err:
    if not is_err_type(err, "ResourceAlreadyExistsException"):
        raise
    print("（已存在，略過）")

print("=== 5) IAM roles ===")
trust = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
})


def ensure_role(name: str) -> str:
    try:
        return iam.get_role(RoleName=name)["Role"]["Arn"]
    except Exception as err:
        if not is_err_type(err, "NoSuchEntityException"):
            raise
        role = iam.create_role(RoleName=name, AssumeRolePolicyDocument=trust)["Role"]
        print(f"建立 role: {name}")
        return role["Arn"]


exec_role = f"{PROJECT}-execution"
task_role = f"{PROJECT}-task"
state["executionRoleArn"] = ensure_role(exec_role)
state["taskRoleArn"] = ensure_role(task_role)

# 執行角色：拉 ECR 镜像 + 寫 CloudWatch Logs
try:
    iam.attach_role_policy(RoleName=exec_role, PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy")
except Exception as err:
    if not is_err_type(err, "EntityAlreadyExistsException"):
        raise

# 執行角色也要有 Secrets Manager 讀取權限：task definition 的 secrets[] 是「執行角色」取的，
# 不是 task role。AmazonECSTaskExecutionRolePolicy 只有 ECR pull 與 logs，不含 secrets，
# 所以任務會卡在 ResourceInitializationError: unable to retrieve secret from asm。
exec_secret_policy = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Action": ["secretsmanager:GetSecretValue", "ssm:GetParameters"],
        "Resource": [arn("secretsmanager", f"secret:{PROJECT}/database*", state["account"]), arn("ssm", "parameter/*", state["account"])],
    }],
})
iam.put_role_policy(RoleName=exec_role, PolicyName=f"{PROJECT}-secret-read", PolicyDocument=exec_secret_policy)
print("執行角色加入 secret 讀取權限（secrets[] 由執行角色取，不是 task role）")

# task role：讀 Secrets Manager / SSM，外加 ecs execute-command 需要的 ssmmessages
app_policy = json.dumps({
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue", "ssm:GetParameter"], "Resource": "*"},
        {
            "Effect": "Allow",
            "Action": [
                "ssm:ListCommands", "ssm:SendCommand",
                "ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel",
                "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel",
            ],
            "Resource": "*",
        },
    ],
})
iam.put_role_policy(RoleName=task_role, PolicyName=f"{PROJECT}-app", PolicyDocument=app_policy)

print("=== 6) security groups（task SG + ALB SG）===")


def ensure_sg(name: str, description: str) -> str:
    def find():
        return (ec2.describe_security_groups(
            Filters=[{"Name": "vpc-id", "Values": [vpc["VpcId"]]}, {"Name": "group-name", "Values": [name]}]
        ).get("SecurityGroups") or [None])[0]

    existing = find()
    if existing:
        return existing["GroupId"]
    try:
        created = ec2.create_security_group(GroupName=name, Description=description, VpcId=vpc["VpcId"])
        print(f"建立 SG: {name}")
        return created["GroupId"]
    except Exception as err:
        # describe 有讀寫一致性問題：剛建好的 SG 可能查不到，但 create 會說 duplicate
        if not is_err_type(err, "InvalidGroup.Duplicate"):
            raise
        again = find()
        if not again:
            raise
        print(f"SG 已存在（describe 沒查到，改用 create 例外後重查）: {name}")
        return again["GroupId"]


def authorize(group_id: str, port: int, source: dict) -> None:
    try:
        ec2.authorize_security_group_ingress(
            GroupId=group_id, IpProtocol="tcp", FromPort=port, ToPort=port, **source
        )
        print(f"  ingress {port} ← {source.get('SourceGroup') or source.get('CidrIp')}（寫入 {group_id}）")
    except Exception as err:
        if not is_err_type(err, "InvalidPermissionDuplicate"):
            raise
        print(f"  ingress {port}（規則已存在，略過）")


task_sg = ensure_sg(f"{PROJECT}-task", "ECS Fargate task ENI")
authorize(task_sg, PORT, {"SourceGroup": task_sg})
authorize(task_sg, PORT, {"CidrIp": vpc["CidrBlock"]})   # ALB 的 health check 來源就是 VPC 內
state["taskSgId"] = task_sg

# ALB 如果沒有指定 SG，AWS 會套用 VPC 的 default SG（只有 self-reference）：
# 外部 client 打 80 會被丟包，但 ALB 打到 task 的 health check 仍然 healthy → 「控制面全綠、資料面不通」
alb_sg = ensure_sg(f"{PROJECT}-alb", "ALB public ingress")
authorize(alb_sg, 80, {"CidrIp": "0.0.0.0/0"})
state["albSgId"] = alb_sg

print("=== 7) ALB + target group + listener ===")
alb = next((l for l in elb.describe_load_balancers().get("LoadBalancers", []) if l["LoadBalancerName"] == PROJECT), None)
if alb:
    print(f"ALB 已存在: {alb['LoadBalancerArn']}")
    # 既有 ALB 的 SG 建立後不能改（只能重建）。已經跑著的 ALB 若是 default SG，就把 80 補開在它目前的 SG 上
    if alb_sg not in (alb.get("SecurityGroups") or []):
        print(f"  注意：既有 ALB 用的 SG 是 {','.join(alb.get('SecurityGroups') or [])}")
        for sg_id in alb.get("SecurityGroups") or []:
            authorize(sg_id, 80, {"CidrIp": "0.0.0.0/0"})
else:
    try:
        created = elb.create_load_balancer(
            Name=PROJECT, Type="application", Subnets=subnets, Scheme="internet-facing", SecurityGroups=[alb_sg]
        )
    except Exception as err:
        if is_err_type(err, "AccessDenied") and "CreateServiceLinkedRole" in str(err):
            print("ALB 需要 service-linked role AWSServiceRoleForElasticLoadBalancing：")
            print("  A) policy 加 iam:CreateServiceLinkedRole")
            print("  B) 或 Console → IAM → Roles → Create service-linked role → Elastic Load Balancing（做過一次後就不需要）")
        raise
    alb = created["LoadBalancers"][0]
    for _ in range(30):
        described = elb.describe_load_balancers(LoadBalancerArns=[alb["LoadBalancerArn"]]).get("LoadBalancers") or []
        if not described:
            time.sleep(5)
            continue
        if described[0]["State"] == "active":
            alb = described[0]
            break
        time.sleep(5)
    print(f"ALB: {alb['LoadBalancerArn']}")

tg = next((t for t in elb.describe_target_groups().get("TargetGroups", []) if t["TargetGroupName"] == PROJECT), None)
if not tg:
    created_tg = elb.create_target_group(
        Name=PROJECT,
        Protocol="HTTP",
        Port=PORT,
        VpcId=vpc["VpcId"],
        TargetType="ip",
        HealthCheckPath="/api/health",
        HealthCheckProtocol="HTTP",
        DeregistrationDelayTimeout={"Seconds": 60},
    )
    tg = created_tg["TargetGroups"][0]
    print(f"建立 target group: {tg['TargetGroupArn']}")

listeners = elb.describe_listeners(LoadBalancerArn=alb["LoadBalancerArn"]).get("Listeners", [])
if not listeners:
    elb.create_listener(
        LoadBalancerArn=alb["LoadBalancerArn"],
        Protocol="HTTP",
        Port=80,
        DefaultActions=[{"Type": "forward", "TargetGroupArn": tg["TargetGroupArn"]}],
    )
    print("建立 listener :80")

state["albArn"] = alb["LoadBalancerArn"]
state["albDns"] = alb["DNSName"]
state["targetGroupArn"] = tg["TargetGroupArn"]
print(f"正式網址: http://{state['albDns']}")

print("=== 8) task definition（第一版）===")
td = ecs.register_task_definition(build_task_definition(state, state["ecrUri"]))["taskDefinition"]
state["taskFamily"] = td["family"]
state["taskRevision"] = td["revision"]
print(f"task definition: {td['taskDefinitionArn']}")

print("=== 9) ECS cluster + service ===")
cluster = (ecs.describe_clusters(clusters=[PROJECT]).get("clusters") or [None])[0]
if not cluster:
    created_cluster = ecs.create_cluster(clusterName=PROJECT)
    cluster = created_cluster.get("cluster") or (created_cluster.get("clusters") or [None])[0]
    if not cluster:
        raise SystemExit(f"CreateCluster 回應異常: {created_cluster}")
    print(f"建立 cluster: {cluster['clusterName']}")

service_arns = ecs.list_services(cluster=PROJECT).get("serviceArns", [])
svc = (ecs.describe_services(cluster=PROJECT, services=service_arns).get("services") or [None])[0] if service_arns else None

if not svc:
    created_svc = ecs.create_service(
        cluster=PROJECT,
        serviceName=PROJECT,
        taskDefinition=td["taskDefinitionArn"],
        launchType="FARGATE",
        platformVersion="LATEST",   # '1.0.0' 會被拒（Fargate 最低 1.1.0）
        desiredCount=1,
        networkMode="awsvpc",
        # TargetType 'ip'（awsvpc）時 loadBalancers 一定要帶 containerName/containerPort
        loadBalancers=[{
            "targetGroupArn": tg["TargetGroupArn"],
            "containerName": td["containerDefinitions"][0]["name"],
            "containerPort": PORT,
        }],
        enableExecuteCommand=True,
        networkConfiguration={
            "awsvpcConfiguration": {"subnets": subnets, "securityGroups": [task_sg], "assignPublicIp": "ENABLED"},
        },
    )
    svc = created_svc.get("service") or (created_svc.get("services") or [None])[0]
    if not svc:
        raise SystemExit(f"CreateService 回應異常: {created_svc}")
    print(f"建立 service: {svc['serviceName']}（SG: {task_sg}）")

state.update({"cluster": cluster["clusterName"], "service": svc["serviceName"], "serviceArn": svc["serviceArn"]})

save_state(state)
print("")
print("=== bootstrap 完成 ===")
print("以後每次部署只需：python aws/deploy.py")
print("Neon 的連線字串放 secret：python aws/secret.py（或 python aws/neon.py 先看連線字串）")
