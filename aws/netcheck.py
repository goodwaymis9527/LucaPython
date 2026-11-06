r"""netcheck.py — 網路資料面診斷：SG 規則、NACL、路由表關聯、ALB 的 public 對應

對應 Node 版 aws/netcheck.js。執行：
  python aws/netcheck.py

每一項 separately try/except：IAM 政策少哪個動作就只缺那一項，不會整個腳本掛掉。
"""
import json
import sys

from aws_common import call, print_c, print_h, read_state


def safe(label, fn):
    try:
        fn()
    except Exception as err:
        print_c(f"{label} skipped : {err}（政策缺這個動作）")


def main():
    state = read_state()
    if not state.get("vpcId"):
        raise SystemExit("state 裡沒有 vpcId；先跑 python aws/bootstrap.py 或 python aws/rds.py")
    by_vpc = [{"Name": "vpc-id", "Values": [state["vpcId"]]}]

    safe("ec2:DescribeSecurityGroups", lambda: [
        [print_c(f"SG: {g['GroupId']} {g['GroupName']}"),
         # unmanaged 規則在 Permissions、authorize 寫進去的是 IpPermissions（受管）：
         # 只看 Permissions 會把「有規則的 SG」讀成空陣列
         print_c(f"  managed   : {json.dumps(g.get('IpPermissions'))}"),
         print_c(f"  unmanaged : {json.dumps(g.get('Permissions'))}")]
        for g in call("ec2", "describe_security_groups", Filters=by_vpc).get("SecurityGroups", [])
    ])

    safe("ec2:DescribeNetworkAcls", lambda: [
        [print_c(f"NACL: {a['NetworkAclId']} default={a['IsDefault']} "
                 f"{json.dumps([{'dir': e['Direction'], 'num': e['RuleNumber'], 'proto': e['Protocol'], 'cidr': e['CidrBlock'], 'allow': e['Allow']} for e in a.get('NetworkAclEntries', [])])}"),
         print_c(f"  assoc: {json.dumps([x['AssociationId'] for x in a.get('NetworkAclAssociations', [])])}")]
        for a in call("ec2", "describe_network_acls", Filters=by_vpc).get("NetworkAcls", [])
    ])

    safe("ec2:DescribeSubnets", lambda: [
        print_c(f"subnet: {s['SubnetId']} {s['AvailabilityZone']} publicIpOnLaunch={s.get('MapPublicIpOnLaunch')}")
        for s in call("ec2", "describe_subnets", Filters=by_vpc).get("Subnets", [])
    ])

    safe("ec2:DescribeRouteTables", lambda: [
        [print_c(f"RT: {t['RouteTableId']} default={t['Default']} routes="
                 f"{json.dumps([{'d': r.get('DestinationCidrBlock'), 'gw': r.get('GatewayId'), 'state': r.get('State')} for r in t.get('Routes', [])])}"),
         print_c(f"  assoc: {json.dumps([{'id': a['AssociationId'], 'subnet': a.get('SubnetId'), 'main': a.get('Main')} for a in t.get('Associations', [])])}")]
        for t in call("ec2", "describe_route_tables", Filters=by_vpc).get("RouteTables", [])
    ])

    safe("elasticloadbalancing:DescribeLoadBalancers", lambda: [
        [print_c(f"ALB: {b['LoadBalancerName']} {b['Scheme']} {b['DNSName']} sg={json.dumps(b.get('SecurityGroups'))}"),
         print_c(f"  zones: {json.dumps([{'zone': z['ZoneName'], 'subnet': z['SubnetId'], 'addresses': z.get('LoadBalancerAddresses')} for z in b.get('AvailabilityZones', [])])}")]
        for b in call("elbv2", "describe_load_balancers",
                      LoadBalancerArns=[state["albArn"]] if state.get("albArn") else {}).get("LoadBalancers", [])
    ])

    safe("ec2:DescribeNetworkInterfaces", lambda: [
        print_c(f"ENI: {i['NetworkInterfaceId']} {i['PrivateIpAddress']} "
                f"public={(i.get('Association') or {}).get('PublicIp', 'none')} "
                f"sg={','.join(g['GroupId'] for g in i.get('Groups', []))} {i.get('Description')}")
        for i in call("ec2", "describe_network_interfaces", Filters=by_vpc).get("NetworkInterfaces", [])
    ])
    return 0


if __name__ == "__main__":
    sys.exit(main())
