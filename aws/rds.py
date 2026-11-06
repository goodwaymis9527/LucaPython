r"""rds.py — 建立 RDS PostgreSQL（正式區資料庫）與它的 security group（boto3 版）

做四件事：
  1) 找 default VPC / subnets，必要時建 RDS DB subnet group
  2) 建 SG：luca-python-task（Fargate 任務，8000）與 luca-python-rds（只放行來自 task SG 的 5432）
  3) 建 RDS instance（private，不公開存取）
  4) 把 rdsEndpoint / SG id 寫進 .aws-state/config.json

執行（密碼走環境變數、--password，或 --password-file；不要寫死在檔內）：
  $env:RDS_PASSWORD = '你的密碼'
  python aws/rds.py
  python aws/rds.py --password-file=C:\Users\g01943\secrets\rds.txt
  python aws/rds.py --instance-class=db.t4g.micro --storage=20

注意：本專案預設用 Neon（見 aws/neon.py）。這支腳本是「自己管 RDS」的方案，
對應 Node 版的流程；Neon 與 RDS 兩條路只要選一條，不要同時用。
"""
import os
import sys

from aws_common import PORT, PROJECT, arn, call, new_state, print_c, print_h, save_state, whoami


def opt(name, dflt=None):
    a = next((x for x in sys.argv[1:] if x.startswith(f"--{name}=")), None)
    return a.split("=", 1)[1] if a else dflt


def main():
    db_name = opt("dbname", "awsLucaPysn")
    user = opt("user", "luca")
    password_file = opt("password-file")
    # 三種傳法：--password= / $env:RDS_PASSWORD / --password-file=（檔案內容只有密碼一行）
    password = opt("password") or os.environ.get("RDS_PASSWORD")
    if not password and password_file:
        password = open(password_file, encoding="utf-8").read().strip()
    instance_class = opt("instance-class", "db.t4g.micro")
    storage = int(opt("storage", "20"))

    if not password:
        raise SystemExit("請用 $env:RDS_PASSWORD、--password= 或 --password-file= 傳入密碼（這個值不會寫進倉庫）")

    print_h("=== 1) VPC / subnets / DB subnet group ===")
    vpcs = call("ec2", "describe_vpcs", Filters=[{"Name": "isDefault", "Values": ["true"]}]).get("Vpcs") or []
    if not vpcs:
        raise SystemExit("此區域沒有 default VPC；請先建 VPC 或手動把 vpcId 寫進 config.json")
    vpc = vpcs[0]

    subnets = [s["SubnetId"] for s in call("ec2", "describe_subnets", Filters=[
        {"Name": "vpc-id", "Values": [vpc["VpcId"]]},
        {"Name": "state", "Values": ["available"]},
    ]).get("Subnets", [])]
    if not subnets:
        raise SystemExit("VPC 內沒有可用 subnet")

    state = new_state()
    state["account"] = whoami()
    state.update({"project": PROJECT, "vpcId": vpc["VpcId"], "vpcCidr": vpc["CidrBlock"], "subnets": subnets})
    print_c(f"VPC {vpc['VpcId']}  {vpc['CidrBlock']}  subnet 數={len(subnets)}")

    groups = call("rds", "describe_db_subnet_groups").get("DBSubnetGroups") or []
    sn_group = next((g for g in groups if g["DBSubnetGroupName"] == PROJECT), None)
    sn_group = sn_group or next((g for g in groups if g.get("VpcId") == vpc["VpcId"]), None)
    if not sn_group:
        sn_group = call("rds", "create_db_subnet_group", DBSubnetGroupName=PROJECT,
                        DBSubnetGroupDescription="luca-python RDS subnets", SubnetIds=subnets)["DBSubnetGroup"]
        print_c(f"建立 DB subnet group: {sn_group['DBSubnetGroupName']}")
    else:
        print_c(f"DB subnet group: {sn_group['DBSubnetGroupName']}")

    print_h("=== 2) Security groups ===")

    def find_sg(name):
        return (call("ec2", "describe_security_groups", Filters=[
            {"Name": "vpc-id", "Values": [vpc["VpcId"]]},
            {"Name": "group-name", "Values": [name]},
        ]).get("SecurityGroups") or [None])[0]

    def ensure_sg(name, description):
        existing = find_sg(name)
        if existing:
            return existing["GroupId"]
        try:
            created = call("ec2", "create_security_group", GroupName=name, Description=description, VpcId=vpc["VpcId"])
            print_c(f"建立 SG: {name}")
            return created["GroupId"]
        except Exception as err:
            # describe 讀寫一致性：剛建好的 SG 可能查不到，但 create 會說 duplicate
            if "InvalidGroup.Duplicate" not in str(err):
                raise
            again = find_sg(name)
            if not again:
                raise
            print_c(f"SG 已存在（describe 沒查到，改用 create 例外後重查）: {name}")
            return again["GroupId"]

    task_sg = ensure_sg(f"{PROJECT}-task", "ECS Fargate task ENI")
    rds_sg = ensure_sg(f"{PROJECT}-rds", "RDS PostgreSQL")

    # task SG 要讓 ALB 打到 8000；RDS 只接受來自 task SG 的 5432
    try:
        for params in (
            {"GroupId": task_sg, "IpProtocol": "tcp", "FromPort": PORT, "ToPort": PORT, "SourceGroup": task_sg},
            {"GroupId": task_sg, "IpProtocol": "tcp", "FromPort": PORT, "ToPort": PORT, "CidrIp": vpc["CidrBlock"]},
            {"GroupId": rds_sg, "IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432, "SourceGroup": task_sg},
        ):
            call("ec2", "authorize_security_group_ingress", **params)
    except Exception as err:
        if "InvalidPermissionDuplicate" not in str(err):
            raise
        print_c("（SG 規則已存在，略過）")

    state["taskSgId"] = task_sg
    state["rdsSgId"] = rds_sg

    print_h("=== 3) RDS instance ===")
    insts = call("rds", "describe_db_instances").get("DBInstances") or []
    inst = next((d for d in insts if d["DBInstanceIdentifier"] == PROJECT), None)
    if inst:
        print_c(f"RDS 已存在: {inst['DBInstanceIdentifier']}")
    else:
        inst = call("rds", "create_db_instance", DBInstanceIdentifier=PROJECT, Engine="postgres",
                    EngineVersion="17", DBName=db_name, MasterUsername=user, MasterUserPassword=password,
                    DBInstanceClass=instance_class, AllocatedStorage=storage, StorageType="gp3",
                    VpcSecurityGroupIds=[rds_sg], DBSubnetGroupName=sn_group["DBSubnetGroupName"],
                    PubliclyAccessible=False, MultiAZ=False, StorageEncrypted=True, BackupRetentionPeriod=7)["DBInstance"]
        print_c("RDS 建立中（約 5–10 分鐘）；用 python aws/status.py 或控制台看狀態")

    if inst.get("Endpoint"):
        state["rdsEndpoint"] = inst["Endpoint"]["Address"]
    state["rdsDbName"] = inst.get("DBName") or db_name
    state["rdsUser"] = inst.get("MasterUsername") or user
    save_state(state)

    print()
    print_c(f"endpoint: {state.get('rdsEndpoint') or '（尚未 available，稍後再看）'}")
    secret_arn = arn("secretsmanager", f"{PROJECT}/database", state["account"])
    print_c(f"下一步：python aws/secret.py（secret 會建立成 {secret_arn}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
